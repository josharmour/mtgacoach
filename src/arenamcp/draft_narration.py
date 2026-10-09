"""Spoken draft commentary: why each pick, and where the draft is heading.

Every claim comes from data the pick already used — 17Lands ratings, the
ranking's own reasons, the cards' mana against our lane, and the set primer's
archetypes and pair win rates. A model's reason is spoken only when the
rules-text check accepted it, and never as deck fit for a card outside our
colors. Pick-Two lines give each card its own short reason, so the line says
what each card does for the deck we are drafting rather than repeating that
one of them rates well. Lines stay short: the next pick's speech replaces
this one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from arenamcp.draft_autopick import Lane, PickScore, is_nonbasic_land, is_removal, land_colors, lane_fit
from arenamcp.draft_plan import PlanColors
from arenamcp.draft_plan import plan_colors as _plan_colors
from arenamcp.limited_rules import rules_profile

logger = logging.getLogger(__name__)

COLOR_NAMES = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}
ROLE_WORDS = {
    "payoffs": "payoff",
    "enablers": "enabler",
    "key_commons": "key common",
    "key_uncommons": "key uncommon",
}
# A lane is spoken as "our deck" once the pool leans this firmly (the lane story's bar).
LANE_SETTLED = 0.3
# Model clauses that talk about the deck are preferred over generic praise.
DECK_WORDS = re.compile(r"\b(?:our|lane|deck|plan|needed|needs|curve|mana)\b", re.IGNORECASE)
PAIR_CODE = re.compile(r"\b([WUBRG])/?([WUBRG])\b")


def _colors(code: str) -> str:
    return "-".join(COLOR_NAMES[c] for c in code if c in COLOR_NAMES) or "no color yet"


def _pair_key(code: str) -> str:
    return "".join(c for c in "WUBRG" if c in code)


def _short(name: str) -> str:
    """How a card is said in a list: "Traxos, Academy Guardian" -> "Traxos"."""
    head = name.split(" // ")[0]
    return head.split(",")[0].strip() or name


def _expand_codes(text: str) -> str:
    """Say "UB" or "U/B" as "blue-black" (the speech engine spells codes out)."""

    def expand(match: re.Match) -> str:
        first, second = match.groups()
        return match.group(0) if first == second else _colors(_pair_key(first + second))

    return PAIR_CODE.sub(expand, text)


def plan_colors(plan: str, primer: Any = None) -> str:
    """The color pair a deck plan names, or "" when it names none or only one.

    "Izzet spells with Jace", "the Dimir Threshold Mill deck", "blue-red tempo"
    and "UR spells" all give "UR"/"UB"; a splash is not a lane color and a
    plan that is still open ("Izzet or Azorius", "staying open to Dimir") does
    not commit to the alternatives. See ``arenamcp.draft_plan``.
    """
    main = plan_reading(plan, primer).main
    return main if len(main) == 2 else ""


def plan_reading(plan: str, primer: Any = None) -> PlanColors:
    """Main colors, splash and archetype a plan names (``main`` may be one color)."""
    return _plan_colors(plan, getattr(primer, "archetypes", None) or [])


@dataclass
class _Reason:
    text: str  # follows "Taking Card: " or "Card — "
    kind: str
    plural: str = ""  # the same reason for both cards of a Pick-Two ("both ..."), without "top-rated"


class DraftNarrator:
    """Remembers what it already said so story beats are not repeated."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._lane = ""
        self._plan = PlanColors()
        self._disagreements_told: set[tuple[str, str]] = set()
        self._open_told: set[str] = set()
        self._packs_summarized: set[int] = set()
        self._archetypes_told: set[str] = set()
        self._since_story = 0

    def _planned(self, lane: Lane, model_plan: str, primer: Any, model_lane: str = "") -> Lane:
        """The lane we speak about: the model's stated plan when it names colors, else the pool estimate.

        2026-10-08: the pool estimate tipped to blue-black at P1p9 and P3p1-p4
        while the model's plan stayed Izzet, and the commentary announced a
        Dimir deck and called the Izzet picks off-color. The last plan that
        named colors holds until the model states another; a plan naming one
        color ("blue-based, Izzet or Azorius") keeps the second color open, and
        a named splash is our splash, not off-color.
        """
        stated = plan_reading(model_plan, primer)
        if not stated.main and model_lane and 1 <= len(_pair_key(model_lane)) <= 2:
            stated = PlanColors(main=_pair_key(model_lane))
        if stated.main:
            self._plan = stated
        plan = self._plan
        if not plan.main:
            return lane
        estimate = _pair_key(lane.colors)
        if (
            estimate != plan.main
            and self._settled(lane)
            and (plan.main, estimate) not in self._disagreements_told
        ):
            self._disagreements_told.add((plan.main, estimate))
            name = plan.archetype or (self._archetype_name(primer, plan.main) if len(plan.main) == 2 else "")
            logger.info(
                "Draft lane: the plan says %s%s but the pool estimate says %s; following the plan",
                plan.main,
                f" ({name})" if name else "",
                estimate,
            )
        commitment = max(lane.commitment, LANE_SETTLED) if len(plan.main) == 2 else lane.commitment
        return replace(
            lane,
            colors=plan.main,
            main=plan.main[0],
            commitment=commitment,
            second_hold=1.0 if len(plan.main) == 2 else 0.0,
            splash=plan.splash,
        )

    def pick_line(
        self,
        *,
        names: list[str],
        chosen: list[PickScore],
        ranking: list[PickScore],
        pool: list[int],
        lane_before: Lane,
        lane_after: Lane,
        primer: Any,
        pack_number: int,
        pick_number: int,
        pack_size: int,
        model_reason: str = "",
        model_reasons: list[str] | None = None,
        mana_costs: dict[int, str] | None = None,
        verb: str = "Taking",
        model_plan: str = "",
        model_lane: str = "",
    ) -> str:
        """``verb`` is "Choosing" when the line is spoken before the pick is confirmed.

        ``model_plan`` is the deck plan the model last stated; when it names
        colors, those are our lane for every line until a later plan names
        others, whatever the pool's color counts estimate. ``model_lane`` is
        the advisor's validated lane code, used only for a plan naming no colors.
        """
        if pack_number == 1 and pick_number == 1:
            self.reset()
        lane_after = self._planned(lane_after, model_plan, primer, model_lane)
        if model_reasons is None:
            model_reasons = [model_reason] + [""] * max(0, len(chosen) - 1)
        line = self._lead(verb, names, chosen, ranking, lane_after, primer, model_reasons, mana_costs or {})
        story = self._story(
            pool, lane_before, lane_after, primer, pack_number, pick_number, pack_size, ranking
        )
        self._since_story = 0 if story else self._since_story + 1
        return f"{line} {story}" if story else line

    # -- why these cards -----------------------------------------------------------

    def _lead(self, verb, names, chosen, ranking, lane, primer, model_reasons, mana) -> str:
        if not chosen:
            return f"{verb} {' and '.join(names)}."
        labels = names if len(names) == len(chosen) else [pick.name for pick in chosen]
        quality = self._quality_ranks(ranking, primer)
        reasons = [
            self._why(
                pick,
                index,
                lane,
                primer,
                mana,
                model_reasons[index] if index < len(model_reasons) else "",
                quality,
                several=len(chosen) > 1,
                own_names=labels,
            )
            for index, pick in enumerate(chosen)
        ]
        if len(chosen) == 1:
            return f"{verb} {labels[0]}: {reasons[0].text}."
        shorts = [_short(label) for label in labels]
        both = f"{verb} {shorts[0]} and {shorts[1]}"
        if len(chosen) == 2 and all(r.kind == "quality" for r in reasons):
            if sorted(quality.get(p.grp_id, 99) for p in chosen) in ([1, 1], [1, 2]):
                text = "the two best cards in this pack on 17Lands data"
                if self._settled(lane) and all(
                    self._fit(p, lane, primer, mana) in ("in", "colorless") for p in chosen
                ):
                    text += f", both in our {_colors(lane.colors)} colors"
                return f"{both}: {text}."
        if len(chosen) == 2 and reasons[0].plural and reasons[0].plural == reasons[1].plural:
            text = reasons[0].plural
            if sorted(quality.get(p.grp_id, 99) for p in chosen) in ([1, 1], [1, 2]):
                text += ", and the two best cards in this pack on 17Lands data"
            return f"{both}: {text}."
        parts = [f"{short} — {reason.text}" for short, reason in zip(shorts, reasons, strict=True)]
        return f"{verb} " + "; ".join(parts[:-1]) + f"; and {parts[-1]}."

    @staticmethod
    def _settled(lane: Lane) -> bool:
        return len(lane.colors) == 2 and lane.commitment >= LANE_SETTLED

    @staticmethod
    def _fit(pick, lane, primer, mana) -> str:
        """``lane_fit`` plus "splash": castable only with a color the plan splashes."""
        card = primer.card(pick.grp_id) if primer is not None else None
        if card is None:
            return "unknown"
        fit = lane_fit(card, lane.colors, mana)
        if fit == "off" and lane.splash and lane_fit(card, lane.colors + lane.splash, mana) == "in":
            return "splash"
        return fit

    @staticmethod
    def _quality_ranks(ranking, primer) -> dict[int, int]:
        """17Lands GIH rank among this pack's spells (lands' GIH overrates them)."""
        if primer is None:
            return {}
        rated = []
        for pick in ranking:
            card = primer.card(pick.grp_id)
            if card is not None and card.gih_wr is not None and "Land" not in (card.types or ""):
                rated.append((pick.grp_id, card.gih_wr))
        return {grp_id: 1 + sum(other > gih + 1e-9 for _g, other in rated) for grp_id, gih in rated}

    def _why(self, pick, index, lane, primer, mana, model_reason, quality, *, several, own_names) -> _Reason:
        card = primer.card(pick.grp_id) if primer is not None else None
        notes = " | ".join(pick.reasons)
        settled = self._settled(lane)
        colors = _colors(lane.colors)
        fit = self._fit(pick, lane, primer, mana) if settled else "unknown"
        rank = quality.get(pick.grp_id)

        if card is not None and is_nonbasic_land(card):
            produced = land_colors(card)
            if settled and fit == "fixing":
                return _Reason(f"fixes our {colors} mana", "land", f"both fix our {colors} mana")
            if settled and fit in ("partial", "off") and lane.splash and set(produced) & set(lane.splash):
                shared = "".join(c for c in produced if c in lane.colors + lane.splash)
                return _Reason(f"a {_colors(shared)} source for our {_colors(lane.splash)} splash", "land")
            if settled and fit == "partial":
                shared = "".join(c for c in produced if c in lane.colors)
                return _Reason(f"only a {_colors(shared)} source for our {colors} deck", "land")
            if settled and fit == "off":
                return _Reason(f"a {_colors(produced)} land, outside our colors", "land")
            if len(produced) == 5:
                return _Reason("a land that taps for any color", "land")
            if len(produced) >= 2:
                return _Reason(f"a {_colors(produced)} dual that keeps our options open", "land")
            return _Reason("a utility land", "land")

        top = ", and the top-rated card here" if rank == 1 else ""
        if fit == "splash":
            # The plan names this color as our splash: it is ours, not outside our colors.
            splash = _colors(lane.splash)
            if card is not None and is_removal(card):
                return _Reason(f"removal for our {splash} splash{top}", "splash")
            if rank == 1:
                return _Reason(f"the best card here on 17Lands data, in our {splash} splash", "splash")
            return _Reason(f"a card for our {splash} splash", "splash")
        if fit == "off":
            # Never claim deck fit for a card outside our colors.
            if rank == 1:
                return _Reason(
                    f"the best card here on 17Lands data, though it's outside our {colors} colors", "off"
                )
            return _Reason(f"the strongest option left, though it's outside our {colors} colors", "off")

        deck = f"our {colors} deck" if settled else "our pool"
        if card is not None and is_removal(card):
            if "pool needs removal" in notes:
                return _Reason(f"removal {deck} needs{top}", "removal", f"both removal {deck} needs")
            return _Reason(f"removal that answers creatures{top}", "removal")
        role = self._archetype_role(card, lane, primer)
        if role:
            word, archetype = role
            return _Reason(f"a {word} for {archetype}{top}", "role", f"both {word}s for {archetype}")
        if "early creature curve" in notes:
            curve = f"our {colors} curve" if settled else "our early curve"
            return _Reason(f"a cheap creature for {curve}{top}", "curve", f"both cheap creatures for {curve}")
        if "pool needs more creatures" in notes:
            return _Reason(f"a creature {deck} needs{top}", "creatures", f"both creatures {deck} needs")
        clause = self._model_clause(model_reason, 14 if several else 18, own_names)
        if clause:
            return _Reason(clause, "model")
        open_note = re.search(r"\b([WUBRG]+) looks open", notes)
        if open_note:
            return _Reason(
                f"{_colors(open_note.group(1))} looks open, so cards like this should keep coming", "open"
            )
        if rank == 1:
            text = "the best card in this pack on 17Lands data"
            if settled and fit == "in":
                text += f", in our {colors} colors"
            return _Reason(text, "quality")
        if rank == 2 and several and index > 0:
            return _Reason("the second-best card in this pack on 17Lands data", "quality")
        if settled and fit == "in":
            return _Reason(
                f"a solid card for our {colors} deck", "lane", f"both solid cards for our {colors} deck"
            )
        if settled and fit == "colorless":
            return _Reason(f"a colorless card that fits our {colors} deck", "lane")
        if index > 0:
            return _Reason("the next-best option for our pool", "default")
        return _Reason("the strongest option here for our pool", "default")

    @staticmethod
    def _model_clause(reason: str, limit: int, own_names: list[str]) -> str:
        """One verified clause of the model's reason, preferring one about our deck."""
        reason = (reason or "").strip()
        if not reason or "(synergy unverified)" in reason:
            return ""
        clauses = [
            part.strip().rstrip(".;:, ")
            for part in re.split(r"(?<=[.;:])\s+|\s[—–]\s", reason)
            if part.strip()
        ]
        fitting = [clause for clause in clauses if 3 <= len(clause.split()) <= limit]
        if not fitting:
            return ""
        clause = _expand_codes(next((c for c in fitting if DECK_WORDS.search(c)), fitting[0]))
        first = clause.split()[0]
        proper = first.isupper() or any(_short(name).split()[0] == first for name in own_names if name)
        return clause if proper else clause[0].lower() + clause[1:]

    def _archetype_role(self, card, lane, primer) -> tuple[str, str] | None:
        """(role word, archetype name) when the primer lists this card for our lane's archetype."""
        if card is None or primer is None or not lane.colors:
            return None
        lane_key = _pair_key(lane.colors)
        for colors, role in primer.card_roles(card.name):
            if _pair_key(colors) == lane_key:
                name = self._archetype_name(primer, colors)
                if name:
                    return ROLE_WORDS.get(role, "key card"), name
        return None

    @staticmethod
    def _archetype(primer, colors: str) -> dict | None:
        key = _pair_key(colors)
        return next((a for a in primer.archetypes if _pair_key(a.get("colors", "")) == key), None)

    def _archetype_name(self, primer, colors: str) -> str:
        archetype = self._archetype(primer, colors)
        return str(archetype.get("name") or "") if archetype else ""

    # -- where the draft is heading -------------------------------------------------

    def _story(
        self, pool, lane_before, lane_after, primer, pack_number, pick_number, pack_size, ranking
    ) -> str:
        if primer is None:
            return ""
        last_pick = pick_number >= max(1, pack_size)
        if last_pick and pack_number not in self._packs_summarized and pack_number < 3:
            self._packs_summarized.add(pack_number)
            return self._pack_summary(pool, lane_after, primer, pack_number)
        new_lane = _pair_key(lane_after.colors)
        if len(new_lane) == 2 and new_lane != self._lane and lane_after.commitment >= LANE_SETTLED:
            self._lane = new_lane
            return self._lane_story(new_lane, primer)
        if pack_number <= 2 and 4 <= pick_number <= 10:
            for pick in ranking[:3]:
                for color in re.findall(
                    r"\b([WUBRG])\b looks open|\b([WUBRG]) looks open", " ".join(pick.reasons)
                ):
                    code = "".join(color)
                    if code and code not in self._open_told:
                        self._open_told.add(code)
                        return f"{_colors(code).capitalize()} looks open: strong {_colors(code)} cards are still coming this late."
        if self._since_story >= 4 and lane_after.colors:
            return self._needs(pool, lane_after, primer)
        return ""

    def _lane_story(self, lane: str, primer) -> str:
        rates = {_pair_key(k): row.get("win_rate") for k, row in (primer.pair_stats or {}).items()}
        ranked = sorted((r for r in rates.values() if r), reverse=True)
        archetype = self._archetype(primer, lane)
        text = f"We're leaning {_colors(lane)}"
        if rates.get(lane) and ranked and rates[lane] >= ranked[min(2, len(ranked) - 1)]:
            text += ", one of the strongest pairs in this set on 17Lands"
        if archetype and archetype.get("name") and lane not in self._archetypes_told:
            self._archetypes_told.add(lane)
            text += f"; the deck is {archetype['name']}"
        return text + "."

    def _pool_cards(self, pool, lane, primer):
        cards = [primer.card(g) for g in pool]
        return [c for c in cards if c is not None and (not c.colors or set(c.colors) <= set(lane.colors))]

    def _pack_summary(self, pool, lane, primer, pack_number) -> str:
        on_color = self._pool_cards(pool, lane, primer)
        best = sorted((c for c in on_color if c.gih_wr is not None), key=lambda c: -c.gih_wr)[:2]
        splash = f" splashing {_colors(lane.splash)}" if lane.splash else ""
        text = f"End of pack {pack_number}: we're {_colors(lane.colors)}{splash} with {len(on_color)} on-color cards"
        if best:
            text += f", led by {' and '.join(c.name for c in best)}"
        needs = self._missing(on_color)
        return text + (f"; still looking for {needs}." if needs else ".")

    def _needs(self, pool, lane, primer) -> str:
        """Every few picks: the plan we are drafting toward and what it still lacks."""
        needs = self._missing(self._pool_cards(pool, lane, primer))
        name = self._archetype_name(primer, lane.colors) if len(lane.colors) == 2 else ""
        deck = f"{name} deck" if name else f"{_colors(lane.colors)} deck"
        if needs:
            return f"For our {deck} we still want {needs}."
        return f"Still on plan: {name}." if name else ""

    @staticmethod
    def _missing(cards) -> str:
        removal = sum(is_removal(c) for c in cards)
        early = sum(
            rules_profile({"type_line": c.types, "oracle_text": c.oracle})["unconditional_body"]
            and c.cmc is not None
            and c.cmc <= 2
            for c in cards
        )
        wants = []
        if removal < 3:
            wants.append("removal")
        if early < 3:
            wants.append("two-drops")
        return " and ".join(wants)
