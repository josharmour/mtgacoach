"""Pre-draft playbook: what every card in the set does, and what each archetype is built on.

A strategic drafter knows before P1p1 which mechanics each color pair runs on
(its engine: enablers feeding payoffs), what a finished deck needs, and which
cards fill those roles. This module turns the set primer (17lands set data,
card rules, and the model's pre-draft synthesis) into that playbook:

- Every card in the set gets rules-text tags: the mechanics it enables or pays
  off (``limited_rules.rules_profile``) and its deck role (removal, early body,
  finisher, card advantage, fixing, ...). This covers all cards, not only the
  handful the primer's archetype lists name.
- Every archetype gets an engine: the mechanics with real payoff density in its
  colors. The synthesis may name it ("engine": [...]); otherwise it is derived
  from the cards, with the primer's listed payoffs and enablers voting for the
  mechanics they use.
- ``fit`` scores a card for an archetype from those tags (0 = no use, 1 = an
  engine payoff), independent of its win rate.

Built on demand from a primer and kept on it; nothing here calls the model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.limited_rules import MECHANIC_LABELS, rules_profile
from arenamcp.set_primer import ROLE_KEYS, SetCard, SetPrimer, normalize_colors

# Mechanics that build one engine together: a Jace payoff wants the empower
# cards that also make planeswalkers and loyalty; scry and surveil feed the
# same "whenever you scry or surveil" payoffs.
ENGINE_FAMILIES = {
    "surveil": ("scry", "surveil"),
    "planeswalker": ("planeswalker", "jace", "loyalty"),
}
ENGINE_LABELS = {"surveil": "scry and surveil", "planeswalker": "Jace and planeswalkers"}
_FAMILY = {member: head for head, members in ENGINE_FAMILIES.items() for member in members}

ENGINE_MAX = 2
ENGINE_MIN_ENABLERS = 4
ENGINE_MIN_PAYOFFS = 2
# Every creature enables creature entry and every noncreature spell enables
# noncreature-spell payoffs: those engines need more payoffs to exist, and one
# more of their enablers counts for less.
BROAD_MECHANICS = {
    "creature_entry": (4, 0.4),
    "artifact_entry": (3, 0.5),
    "noncreature_spell": (4, 0.6),
    "instant_sorcery": (4, 0.6),
}
# A listed primer card votes for the mechanics it uses (the synthesis read the set).
LISTED_VOTE = 2

# Static fit: how much the archetype wants this kind of card. What a payoff or
# enabler adds given the pool is ``draft_deck_plan.engine_gain``; fit stays
# modest so the two do not count one fact twice.
FIT_ENGINE_PAYOFF = 0.7
FIT_ENGINE_ENABLER = 0.55
FIT_ENGINE_INDIRECT = 0.3
FIT_LISTED = {"payoffs": 0.75, "enablers": 0.65, "key_uncommons": 0.6, "key_commons": 0.6}
FIT_ROLE = {
    "removal": 0.6,
    "card_advantage": 0.5,
    "early_body": 0.45,
    "finisher": 0.4,
    "evasive_body": 0.35,
    "counterspell": 0.35,
    "body": 0.3,
    "fixing": 0.25,
    "trick": 0.2,
}
FIT_FLOOR = 0.15

# Pack makeup for supply estimates: expected copies per fresh pack by rarity
# (FRA play boosters showed 7-8 commons, 4-5 uncommons, 1-2 rares/mythics).
PACK_SLOTS = {"C": 7.5, "U": 4.5, "R": 1.15, "M": 0.2}
PACK_SIZE = 14
# When 17lands has no last-seen pick for a card: the rarity's FRA median.
DEFAULT_ALSA = {"C": 6.2, "U": 5.0, "R": 3.5, "M": 2.3}
# Picks of spread around the average last-seen pick: wider than the wheel
# prediction (0.8), since a card's last-seen pick varies from draft to draft.
SURVIVAL_SPREAD = 1.5

ROLE_WORDS = {
    "removal": "removal",
    "card_advantage": "card advantage",
    "early_body": "an early creature",
    "finisher": "a finisher",
    "evasive_body": "an evasive creature",
    "counterspell": "a counterspell",
    "body": "a creature",
    "fixing": "mana fixing",
    "trick": "a combat trick",
}

_COUNTERSPELL = re.compile(r"\bcounter target (?:\w+ )?spell\b")
_EXTRA_CARDS = re.compile(
    r"\bdraw (?:two|three|four|x|\d+) cards\b|^whenever [^,]*, (?:you may )?draw a card"
    r"|\bcreate (?:two|three|four|x|\d+) [^.]*creature tokens\b"
)
_EVASION = re.compile(r"^(?:flying|menace|trample|can't be blocked)\b")
_TRICK = re.compile(
    r"\btarget creature (?:you control )?gets \+\d+/\+\d+|\bgains? (?:hexproof|indestructible)\b"
)
_FIXING = re.compile(r"\bbasic landcycling\b|\bsearch your library for a basic land\b")
_PAYOFF_LINE = re.compile(
    r"^prowess\b|^whenever you (?:cast|scry|surveil|draw|gain life|put)\b|you(?:'ve| have) (?:scried|surveilled)"
)
_TEMPORARY_PUMP = re.compile(r"\bgets? \+\d+/\+\d+ until end of turn\b")
_LASTING_REWARD = re.compile(r"\bcreate\b|\bdraw\b|\bdamage\b|\bcounters?\b|\breturn\b|\bcan attack\b")
WEAK_PAYOFF = 0.6


def family(mechanic: str) -> str:
    """The engine a mechanic belongs to (itself unless it is part of a family)."""
    return _FAMILY.get(mechanic, mechanic)


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine) or MECHANIC_LABELS.get(engine, engine)


def _with_article(phrase: str) -> str:
    return ("an " if phrase[:1].lower() in "aeiou" else "a ") + phrase


@dataclass(frozen=True)
class CardTags:
    grp_id: int
    name: str
    colors: str
    cmc: float | None
    roles: frozenset[str]
    enables: frozenset[str]
    payoffs: frozenset[str]
    indirect: frozenset[str]
    land: bool = False
    # How much a payoff gives per trigger: 1.0, or WEAK_PAYOFF when its only
    # reward is a temporary pump (prowess, "+1/+1 until end of turn").
    payoff_strength: float = 1.0
    # A planeswalker card (not a card that only makes or empowers a Jace token).
    planeswalker: bool = False

    def engine_payoffs(self) -> set[str]:
        return {family(m) for m in self.payoffs}

    def engine_enables(self) -> set[str]:
        return {family(m) for m in self.enables}

    def engine_indirect(self) -> set[str]:
        return {family(m) for m in self.indirect} - self.engine_enables()


@dataclass
class ArchetypePlan:
    colors: str
    name: str
    tier: int
    plan: str = ""
    engine: tuple[str, ...] = ()
    win_rate: float | None = None
    # Lowercased card name -> the strongest primer role naming it.
    listed: dict[str, str] = field(default_factory=dict)
    avoid: frozenset[str] = frozenset()
    # Engine -> (enablers, payoffs) among the set's spells in these colors.
    engine_depth: dict[str, tuple[int, int]] = field(default_factory=dict)

    def engine_words(self) -> str:
        return " and ".join(engine_label(m) for m in self.engine)


def _plain(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text or "").replace("’", "'").lower()
    return re.sub(r"\([^)]*\)", "", text)


def card_tags(card: SetCard) -> CardTags:
    """Deck roles and mechanics from a card's type line and rules text."""
    from arenamcp.draft_autopick import is_nonbasic_land, is_removal, land_colors

    profile = rules_profile({"type_line": card.types, "oracle_text": card.oracle})
    text = _plain(card.oracle)
    lines = [line.strip() for line in re.split(r"[.\n]", text) if line.strip()]
    kind = (card.types or "").lower()
    front = kind.split("//")[0]
    land = "land" in front
    roles: set[str] = set()
    if is_removal(card):
        roles.add("removal")
    if _COUNTERSPELL.search(text):
        roles.add("counterspell")
    if any(_EXTRA_CARDS.search(line) for line in lines):
        roles.add("card_advantage")
    if profile["unconditional_body"]:
        roles.add("body")
        if card.cmc is not None and card.cmc <= 2:
            roles.add("early_body")
        if "creature" in front and any(_EVASION.match(line) for line in lines):
            roles.add("evasive_body")
        if card.cmc is not None and card.cmc >= 5:
            roles.add("finisher")
    if "planeswalker" in front:
        roles.add("finisher" if (card.cmc or 0) >= 4 else "card_advantage")
    if is_nonbasic_land(card):
        if len(land_colors(card)) >= 2:
            roles.add("fixing")
    elif _FIXING.search(text):
        roles.add("fixing")
    if "instant" in front and "creature" not in front and _TRICK.search(text) and "removal" not in roles:
        roles.add("trick")
    payoff_lines = [line for line in lines if _PAYOFF_LINE.search(line)]
    weak = bool(payoff_lines) and all(
        line.startswith("prowess") or (_TEMPORARY_PUMP.search(line) and not _LASTING_REWARD.search(line))
        for line in payoff_lines
    )
    return CardTags(
        grp_id=card.grp_id,
        name=card.name,
        colors=card.colors or "",
        cmc=card.cmc,
        roles=frozenset(roles),
        enables=frozenset(profile["enables"]),
        payoffs=frozenset(profile["payoffs"]),
        indirect=frozenset(profile.get("indirect") or ()),
        land=land,
        payoff_strength=WEAK_PAYOFF if weak else 1.0,
        planeswalker="planeswalker" in front,
    )


class Playbook:
    """Card tags and archetype engines for one set primer."""

    def __init__(self, primer: SetPrimer) -> None:
        self.primer = primer
        self.speed = (primer.speed or "medium").lower()
        self.tags: dict[int, CardTags] = {grp_id: card_tags(card) for grp_id, card in primer.cards.items()}
        self.archetypes: list[ArchetypePlan] = []
        for entry in primer.archetypes:
            colors = normalize_colors(entry.get("colors"))
            if len(colors) != 2:
                continue
            self.archetypes.append(self._archetype(colors, entry))
        self.availability = self._availability()

    def _availability(self) -> dict[int, list[float]]:
        """Per card: expected copies still in a pack when it reaches us at each pick (1-14).

        Copies per fresh pack come from its rarity (PACK_SLOTS / cards of that
        rarity); survival to pick e from when drafters last see it (17lands
        ALSA), the same logistic the wheel prediction uses. This is what the
        set can still offer in packs we have not seen yet.
        """
        counts = Counter(card.rarity for card in self.primer.cards.values() if card.rarity in PACK_SLOTS)
        table: dict[int, list[float]] = {}
        for grp_id, card in self.primer.cards.items():
            if card.rarity not in PACK_SLOTS or not counts[card.rarity]:
                continue
            per_pack = PACK_SLOTS[card.rarity] / counts[card.rarity]
            alsa = card.alsa or DEFAULT_ALSA[card.rarity]
            table[grp_id] = [
                per_pack / (1.0 + math.exp(-(alsa - pick) / SURVIVAL_SPREAD))
                for pick in range(1, PACK_SIZE + 1)
            ]
        return table

    # -- queries ----------------------------------------------------------

    def tag(self, grp_id: int) -> CardTags | None:
        return self.tags.get(int(grp_id))

    def archetype(self, colors: str) -> ArchetypePlan | None:
        key = normalize_colors(colors)
        return next((plan for plan in self.archetypes if plan.colors == key), None)

    def fit(
        self,
        grp_id: int,
        plan: ArchetypePlan,
        castable: bool = True,
        *,
        roles: bool = True,
        key_lists: bool = True,
        support: dict[str, float] | None = None,
    ) -> tuple[float, str]:
        """How much this archetype wants the card (0-1), and the reason in words.

        ``support`` (engine -> 0-1) scales a payoff's fit by how well the pool
        can feed that engine (``draft_deck_plan.payoff_support``).

        Rules text decides: an engine payoff or enabler, a card the pre-draft
        synthesis lists for the archetype, or (with ``roles``) a role every
        deck needs. Win rate is not part of fit. The pick score uses
        ``roles=False``: generic roles are its curve and removal needs, so
        archetype credit is only for what this archetype specifically wants.
        ``key_lists=False`` drops the synthesis's key-common/uncommon lists
        (good cards in those colors), keeping engine pieces and listed
        payoffs and enablers.
        """
        tags = self.tag(grp_id)
        if tags is None or not castable:
            return 0.0, ""
        name = tags.name.lower()
        if name in plan.avoid:
            return 0.0, f"{plan.name} avoids it"
        best, why = (FIT_FLOOR if roles else 0.0), ""
        if not tags.land:
            # A dual that enters untapped beside a planeswalker is fixing, not an engine piece.
            payoffs, enables, indirect = tags.engine_payoffs(), tags.engine_enables(), tags.engine_indirect()
            for engine in plan.engine:
                label = engine_label(engine)
                factor = BROAD_MECHANICS.get(engine, (0, 1.0))[1]
                if engine in payoffs:
                    share = (support or {}).get(engine, 1.0)
                    candidate = (FIT_ENGINE_PAYOFF * tags.payoff_strength * share, f"a payoff for {label}")
                elif engine in enables:
                    candidate = (FIT_ENGINE_ENABLER * factor, f"an enabler for {label}")
                elif engine in indirect:
                    candidate = (FIT_ENGINE_INDIRECT * factor, f"an indirect enabler for {label}")
                else:
                    continue
                if candidate[0] > best:
                    best, why = candidate
        role = plan.listed.get(name)
        if role and not key_lists and role.startswith("key_"):
            role = None
        if role and FIT_LISTED[role] > best:
            best = FIT_LISTED[role]
            why = f"{_with_article(role.rstrip('s').replace('_', ' '))} for {plan.name}"
        for role_name, value in FIT_ROLE.items() if roles else ():
            if role_name in tags.roles and value > best:
                best, why = value, ROLE_WORDS[role_name]
        return round(best, 3), why

    def prompt_context(self, colors: list[str]) -> list[dict[str, Any]]:
        """Archetype engines for the pick prompt, in the order given."""
        rows = []
        for key in colors:
            plan = self.archetype(key)
            if plan is None:
                continue
            rows.append(
                {
                    "colors": plan.colors,
                    "name": plan.name,
                    "tier": plan.tier,
                    "engine": [engine_label(m) for m in plan.engine],
                }
            )
        return rows

    # -- construction -----------------------------------------------------

    def _archetype(self, colors: str, entry: dict[str, Any]) -> ArchetypePlan:
        listed: dict[str, str] = {}
        for role in ROLE_KEYS:
            for card_name in entry.get(role) or []:
                key = str(card_name).lower()
                if key not in listed or FIT_LISTED[role] > FIT_LISTED[listed[key]]:
                    listed[key] = role
        depth = self._engine_depth(colors, listed)
        stated = [family(m) for m in entry.get("engine") or [] if family(str(m)) in depth]
        engine = tuple(dict.fromkeys(stated))[:ENGINE_MAX] or self._derive_engine(depth)
        return ArchetypePlan(
            colors=colors,
            name=str(entry.get("name") or colors),
            tier=int(entry.get("tier") or 2),
            plan=str(entry.get("plan") or ""),
            engine=engine,
            win_rate=(self.primer.pair_stats.get(colors) or {}).get("win_rate"),
            listed=listed,
            avoid=frozenset(str(name).lower() for name in entry.get("avoid") or []),
            engine_depth={m: depth[m][:2] for m in engine},
        )

    def _engine_depth(self, colors: str, listed: dict[str, str]) -> dict[str, tuple[int, int, int]]:
        """engine -> (enablers, payoffs, votes) among the spells these colors can play."""
        enablers: Counter = Counter()
        payoffs: Counter = Counter()
        votes: Counter = Counter()
        for tags in self.tags.values():
            if tags.land or (tags.colors and not set(tags.colors) <= set(colors)):
                continue
            # Gold cards are the archetype's signposts: their payoffs count double.
            weight = 2 if len(tags.colors) == 2 else 1
            for engine in tags.engine_payoffs():
                payoffs[engine] += weight
            for engine in tags.engine_enables():
                enablers[engine] += 1
            role = listed.get(tags.name.lower())
            if role in ("payoffs", "enablers"):
                voted = tags.engine_payoffs()
                if role == "enablers":
                    voted |= {
                        m for m in tags.engine_enables() | tags.engine_indirect() if m not in BROAD_MECHANICS
                    }
                for engine in voted:
                    votes[engine] += LISTED_VOTE
        return {m: (enablers[m], payoffs[m], votes[m]) for m in set(enablers) | set(payoffs)}

    @staticmethod
    def _derive_engine(depth: dict[str, tuple[int, int, int]]) -> tuple[str, ...]:
        candidates = [
            engine
            for engine, (enabled, paid, _votes) in depth.items()
            if paid >= BROAD_MECHANICS.get(engine, (ENGINE_MIN_PAYOFFS, 1.0))[0]
            and enabled >= ENGINE_MIN_ENABLERS
        ]
        ranked = sorted(candidates, key=lambda m: (-(depth[m][1] + depth[m][2]), m))
        return tuple(ranked[:ENGINE_MAX])


def playbook_for(primer: SetPrimer | None) -> Playbook | None:
    """The playbook for this primer (built once, rebuilt when the primer is rebuilt or merged)."""
    if primer is None:
        return None
    signature = (primer.built_at, primer.source, len(primer.cards), len(primer.archetypes))
    cached = primer.__dict__.get("_playbook")
    if cached is None or cached[0] != signature:
        # Kept on the primer object, outside its dataclass fields (not serialized).
        cached = (signature, Playbook(primer))
        primer.__dict__["_playbook"] = cached
    return cached[1]
