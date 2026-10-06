"""Spoken draft commentary: why each pick, and where the draft is heading.

Every claim comes from data the pick already used — 17Lands ratings, the
ranking's own reasons, the pool's colors, and the set primer's archetypes and
pair win rates. A model's synergy claim is spoken only when the rules-text
check accepted it. Lines stay short: the next pick's speech replaces this one.
"""

from __future__ import annotations

import re
from typing import Any

from arenamcp.draft_autopick import Lane, PickScore, is_removal
from arenamcp.limited_rules import rules_profile

COLOR_NAMES = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}
ROLE_WORDS = {
    "payoffs": "payoff",
    "enablers": "enabler",
    "key_commons": "key common",
    "key_uncommons": "key uncommon",
}


def _colors(code: str) -> str:
    return "-".join(COLOR_NAMES[c] for c in code if c in COLOR_NAMES) or "no color yet"


def _pair_key(code: str) -> str:
    return "".join(c for c in "WUBRG" if c in code)


class DraftNarrator:
    """Remembers what it already said so story beats are not repeated."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._lane = ""
        self._open_told: set[str] = set()
        self._packs_summarized: set[int] = set()
        self._archetypes_told: set[str] = set()
        self._since_story = 0

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
    ) -> str:
        if pack_number == 1 and pick_number == 1:
            self.reset()
        lead = f"Taking {' and '.join(names)}"
        why = self._why(chosen, ranking, lane_after, primer, model_reason)
        line = f"{lead}: {why}." if why else f"{lead}."
        story = self._story(
            pool, lane_before, lane_after, primer, pack_number, pick_number, pack_size, ranking
        )
        self._since_story = 0 if story else self._since_story + 1
        return f"{line} {story}" if story else line

    # -- why this card ------------------------------------------------------------

    def _why(self, chosen, ranking, lane, primer, model_reason) -> str:
        if not chosen:
            return ""
        pick = chosen[0]
        card = primer.card(pick.grp_id) if primer is not None else None
        rated = [(p, primer.card(p.grp_id)) for p in ranking] if primer is not None else []
        gihs = [c.gih_wr for _p, c in rated if c is not None and c.gih_wr is not None]
        reasons = " | ".join(pick.reasons)
        if card is not None and card.gih_wr is not None and gihs and card.gih_wr >= max(gihs) - 1e-9:
            return "the best card in this pack on 17Lands data"
        if "pool needs removal" in reasons and card is not None and is_removal(card):
            return "we need removal, and this answers creatures"
        if "early creature curve" in reasons:
            return "it fills our early creature curve"
        if "pool needs more creatures" in reasons:
            return "we need more creatures"
        open_note = re.search(r"\b([WUBRG]+) looks open", reasons)
        if open_note:
            return f"{_colors(open_note.group(1))} looks open, so this card should keep coming"
        role = self._archetype_role(card, lane, primer)
        if role:
            return role
        reason = (model_reason or "").strip()
        if reason and "(synergy unverified)" not in reason:
            clause = re.split(r"(?<=[.;:])\s|\s[—–]\s", reason, maxsplit=1)[0].rstrip(".;: ")
            words = clause.split()
            if 3 <= len(words) <= 18:
                return clause[0].lower() + clause[1:]
        if "in lane" in reasons and lane.colors:
            return f"a solid card for our {_colors(lane.colors)} deck"
        return "the strongest option here for our pool"

    def _archetype_role(self, card, lane, primer) -> str:
        if card is None or primer is None or not lane.colors:
            return ""
        lane_key = _pair_key(lane.colors)
        for colors, role in primer.card_roles(card.name):
            if _pair_key(colors) == lane_key:
                name = self._archetype_name(primer, colors)
                if name:
                    return f"a {ROLE_WORDS.get(role, 'key card')} for {name}"
        return ""

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
        if len(new_lane) == 2 and new_lane != self._lane and lane_after.commitment >= 0.3:
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
        text = f"End of pack {pack_number}: we're {_colors(lane.colors)} with {len(on_color)} on-color cards"
        if best:
            text += f", led by {' and '.join(c.name for c in best)}"
        needs = self._missing(on_color)
        return text + (f"; still looking for {needs}." if needs else ".")

    def _needs(self, pool, lane, primer) -> str:
        needs = self._missing(self._pool_cards(pool, lane, primer))
        return f"For our {_colors(lane.colors)} deck we still want {needs}." if needs else ""

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
