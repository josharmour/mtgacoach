"""Synthesize a legal-action menu for a 17lands replay_data row + turn.

SPIKE TOOL. The question this answers: is a legal-action menu reconstructed
from 17lands `replay_data` close enough to a REAL MTGA `ActionsAvailableReq`
menu to train on? A nearly-right menu is worse than no menu -- it teaches the
model to pick from a distribution that does not exist at inference time.

What a 17lands row gives us, per player turn N:
    user_turn_N_cards_drawn                  pipe-separated grpIds
    user_turn_N_lands_played                 pipe-separated grpIds  (a PICK)
    user_turn_N_creatures_cast               pipe-separated grpIds  (a PICK)
    user_turn_N_non_creatures_cast           pipe-separated grpIds  (a PICK)
    user_turn_N_user_abilities               pipe-separated ABILITY ids
    user_turn_N_eot_user_cards_in_hand       pipe-separated grpIds
    user_turn_N_eot_user_lands_in_play       pipe-separated grpIds
    user_turn_N_eot_user_creatures_in_play   pipe-separated grpIds
    user_turn_N_eot_user_non_creatures_in_play
plus game-level `opening_hand`, `on_play`, `num_turns`, `rank`, `won`.

What it does NOT give us: tapped status, within-turn ordering, instance ids,
mana abilities, targets, stack state, or which permanent an ability came from.

The reconstruction under test:
    hand   = (opening_hand if N == 1 else eot hand of turn N-1) + cards drawn on N
    mana   = lands in play at start of N (= eot lands of N-1), ALL UNTAPPED
             (true at the start of a turn -- everything untapped in untap step)
    board  = eot creatures / non-creatures of N-1
    menu   = {Cast c for affordable c in hand} + {Play l for land l in hand}
             + {Activate a for activated abilities on board} + {Pass}

Three affordability policies are measured, because which one is "right"
is itself the finding:
    strict   -- only start-of-turn lands can pay (the literal decision point
                at the top of main phase 1, before the land drop)
    postland -- start-of-turn lands + the best single land from hand
                (models the menu the player actually saw after the land drop,
                since 17lands does not record within-turn ordering)
    nofilter -- no affordability filter at all; every nonland card in hand is
                a Cast entry. This is what REAL MTGA menus do (measured: 50.7%
                of active Cast entries in real replays are unaffordable with
                currently untapped lands -- MTGA gates on timing/zone/targets,
                not on mana, and resolves payment later via autotap).

Output JSONL uses the same normalized (actionType, grpId) set form as the
ground-truth extractor in tools/eval/replay (see the `menu` / `pick` keys in
`Decision.ground_truth`), so synthesized and real menus are directly
comparable.

CPU only. No GPU, no network beyond an already-cached Scryfall bulk file.

Usage:
    python -m tools.training.synthesize_menu \
        --replay-csv tools/eval/data/17lands/replay_data_public.EOE.PremierDraft.csv.gz \
        --min-rank diamond --max-rows 2000 \
        --out /tmp/synth_menus.jsonl
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("synthesize_menu")

# Repo import path (works when run as a module or as a script).
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RANK_ORDER = ["bronze", "silver", "gold", "platinum", "diamond", "mythic"]

CARD_INDEX_PATH = Path.home() / ".arenamcp" / "cache" / "synth_menu_card_index.json"

# ---------------------------------------------------------------------------
# Card data -- sourced from the repo card DB, never hardcoded.
# ---------------------------------------------------------------------------

WUBRG = ("W", "U", "B", "R", "G")


@dataclass
class Card:
    """The slice of card data the menu synthesizer needs."""

    grp_id: int
    name: str = ""
    type_line: str = ""
    mana_cost: str = ""  # front face, or "a // b" for split
    layout: str = ""
    produced_mana: tuple[str, ...] = ()  # for lands
    faces: tuple[str, ...] = ()  # per-face mana costs (MDFC / split)
    face_types: tuple[str, ...] = ()
    resolved: bool = True
    source: str = ""

    @property
    def is_land(self) -> bool:
        return "Land" in self.type_line

    @property
    def has_land_face(self) -> bool:
        return any("Land" in t for t in self.face_types) or self.is_land

    @property
    def is_creature(self) -> bool:
        return "Creature" in self.type_line

    @property
    def castable_costs(self) -> list[str]:
        """Every mana cost this card could be cast for (one per castable face)."""
        out: list[str] = []
        if self.faces:
            for cost, tl in zip(self.faces, self.face_types, strict=False):
                if "Land" in tl:
                    continue  # land faces are Play, not Cast
                out.append(cost)
        elif not self.is_land:
            out.append(self.mana_cost)
        return [c for c in out if c is not None]


class CardResolver:
    """grpId -> Card, backed by the repo's Scryfall bulk cache + MTGA local DB.

    Scryfall is the only source in the repo that carries `mana_cost` and
    `produced_mana` keyed by arena_id; the MTGA local DB has names/types but
    explicitly stores no mana cost (see card_db.MTGADatabaseAdapter). Both are
    used: Scryfall first, MTGA DB as the name/type fallback so that tokens and
    Alchemy-rebalanced grpIds are still identified rather than silently dropped.

    A distilled {grpId: fields} index is cached on disk so repeat runs are fast.
    """

    def __init__(self, index_path: Path = CARD_INDEX_PATH, allow_network: bool = False):
        self._index_path = index_path
        self._allow_network = allow_network
        self._cache: dict[int, Card] = {}
        self._distilled: dict[str, dict] = {}
        self._scry_raw: dict[int, dict] = {}
        self._scry_by_name: dict[str, dict] = {}
        self._mtga_db = None
        self._dirty = False
        self._load()

    @staticmethod
    def _build_name_index(cache) -> dict[str, dict]:
        path = cache._get_bulk_data_path()
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as fh:
            cards = json.load(fh)
        idx: dict[str, dict] = {}
        for c in cards:
            name = (c.get("name") or "").lower()
            if name and name not in idx:
                idx[name] = c
        logger.info("scryfall name index: %d names", len(idx))
        return idx

    # -- setup ------------------------------------------------------------

    def _load(self) -> None:
        if self._index_path.exists():
            try:
                self._distilled = json.loads(self._index_path.read_text())
                logger.info("loaded distilled card index: %d entries", len(self._distilled))
            except Exception as exc:  # noqa: BLE001
                logger.warning("distilled index unreadable (%s); rebuilding", exc)
                self._distilled = {}

        # Scryfall bulk (offline). Pin the staleness window so a cached bulk
        # file is never re-downloaded just to run a CPU-only spike.
        try:
            import arenamcp.scryfall as scryfall_mod

            if not self._allow_network:
                scryfall_mod.CACHE_MAX_AGE_HOURS = 24 * 365 * 100
            cache = scryfall_mod.ScryfallCache()
            cache._thread.join(timeout=600)
            self._scry_raw = cache._arena_index
            logger.info("scryfall arena index: %d cards", len(self._scry_raw))
            # `default_cards` keeps one printing per oracle card, so only ONE
            # arena_id survives per card. MTGA ships many alternate-art
            # grpIds for the same card (every basic land, every Alchemy
            # reprint), and those grpIds miss the arena index entirely. Build
            # a name index from the same cached bulk file so grpId -> name
            # (MTGA local DB) -> cost/type/produced_mana still resolves.
            self._scry_by_name = self._build_name_index(cache)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scryfall unavailable: %s", exc)

        try:
            from arenamcp.mtgadb import MTGADatabase

            db = MTGADatabase()
            self._mtga_db = db if db.available else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("MTGA local DB unavailable: %s", exc)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            self._index_path.write_text(json.dumps(self._distilled))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not save distilled index: %s", exc)

    # -- lookup -----------------------------------------------------------

    def get(self, grp_id: int) -> Card:
        c = self._cache.get(grp_id)
        if c is not None:
            return c
        c = self._build(grp_id)
        self._cache[grp_id] = c
        return c

    def _build(self, grp_id: int) -> Card:
        key = str(grp_id)
        d = self._distilled.get(key)
        if d is None:
            d = self._distill(grp_id)
            self._distilled[key] = d
            self._dirty = True
        return Card(
            grp_id=grp_id,
            name=d.get("name", ""),
            type_line=d.get("type_line", ""),
            mana_cost=d.get("mana_cost", ""),
            layout=d.get("layout", ""),
            produced_mana=tuple(d.get("produced_mana") or ()),
            faces=tuple(d.get("faces") or ()),
            face_types=tuple(d.get("face_types") or ()),
            resolved=bool(d.get("resolved")),
            source=d.get("source", ""),
        )

    @staticmethod
    def _from_scryfall(raw: dict, source: str) -> dict:
        faces = raw.get("card_faces") or []
        mana_cost = raw.get("mana_cost") or ""
        if not mana_cost and faces:
            mana_cost = " // ".join(f.get("mana_cost", "") for f in faces)
        return {
            "name": raw.get("name", ""),
            "type_line": raw.get("type_line", ""),
            "mana_cost": mana_cost,
            "layout": raw.get("layout", ""),
            "produced_mana": raw.get("produced_mana") or [],
            "faces": [f.get("mana_cost", "") for f in faces],
            "face_types": [f.get("type_line", "") for f in faces],
            "resolved": True,
            "source": source,
        }

    def _distill(self, grp_id: int) -> dict:
        raw = self._scry_raw.get(grp_id)
        if raw is not None:
            return self._from_scryfall(raw, "scryfall-arena")
        mtga_card = self._mtga_db.get_card(grp_id) if self._mtga_db is not None else None
        if mtga_card is not None and mtga_card.name:
            by_name = self._scry_by_name.get(mtga_card.name.lower())
            if by_name is not None:
                return self._from_scryfall(by_name, "scryfall-by-name")
            # MTGA DB has a name but its `Types` column is a numeric code,
            # not a type line, and it stores no mana cost. Identified but
            # not costable -- the "cost could not be resolved" bucket.
            return {
                "name": mtga_card.name,
                "type_line": "",
                "mana_cost": "",
                "layout": "",
                "produced_mana": [],
                "faces": [],
                "face_types": [],
                "resolved": False,
                "source": "mtgadb-name-only",
            }
        return {
            "name": "",
            "type_line": "",
            "mana_cost": "",
            "layout": "",
            "produced_mana": [],
            "faces": [],
            "face_types": [],
            "resolved": False,
            "source": "unresolved",
        }

    def ability_text(self, ability_id: int) -> str:
        if self._mtga_db is None:
            return ""
        try:
            return self._mtga_db.get_ability_text(ability_id) or ""
        except Exception:  # noqa: BLE001
            return ""

    def card_ability_ids(self, grp_id: int) -> list[int]:
        """AbilityIds for a permanent, from the MTGA local DB."""
        if self._mtga_db is None or not self._mtga_db.available:
            return []
        try:
            conn = self._mtga_db._conn
            if conn is None:
                return []
            row = conn.execute("SELECT AbilityIds FROM Cards WHERE GrpId = ?", (int(grp_id),)).fetchone()
        except Exception:  # noqa: BLE001
            return []
        if not row or not row["AbilityIds"]:
            return []
        # Format is comma-separated "AbilityGrpId:TextId" pairs (see
        # mtgadb.MTGADatabase._resolve_oracle_text). The first component is
        # the ability id that 17lands' `user_abilities` column reports.
        out = []
        for pair in str(row["AbilityIds"]).split(","):
            head = pair.split(":")[0].strip()
            if head.isdigit():
                out.append(int(head))
        return out


# ---------------------------------------------------------------------------
# Mana cost parsing + affordability
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


@dataclass
class ManaCost:
    generic: int = 0
    pips: list[frozenset[str]] = field(default_factory=list)  # each pip: acceptable colors
    has_x: bool = False
    parse_ok: bool = True
    note: str = ""

    @property
    def total(self) -> int:
        return self.generic + len(self.pips)


def parse_mana_cost(cost: str) -> ManaCost:
    """Parse a Scryfall mana-cost string into generic + colored pips.

    Handles: {2}, {W}, {W/U} hybrid, {2/W} twobrid, {W/P} phyrexian,
    {C} colorless-specific, {X}, {S} snow. Anything unrecognized flips
    parse_ok=False so it lands in the unresolved bucket rather than
    silently producing a wrong menu.
    """
    mc = ManaCost()
    if cost is None:
        mc.parse_ok = False
        mc.note = "none"
        return mc
    cost = cost.strip()
    if not cost:
        # A genuinely free spell is rare; empty usually means "no cost data".
        mc.parse_ok = False
        mc.note = "empty"
        return mc
    syms = _SYMBOL_RE.findall(cost)
    if not syms:
        mc.parse_ok = False
        mc.note = f"unparsable:{cost[:24]}"
        return mc
    for s in syms:
        su = s.upper()
        if su.isdigit():
            mc.generic += int(su)
        elif su == "X":
            mc.has_x = True  # X=0 is always a legal choice
        elif su in WUBRG:
            mc.pips.append(frozenset({su}))
        elif su == "C":
            mc.pips.append(frozenset({"C"}))
        elif su == "S":
            mc.generic += 1  # snow: approximate as generic
        elif "/" in su:
            parts = su.split("/")
            if "P" in parts:  # phyrexian: payable with life
                continue
            if any(p.isdigit() for p in parts):  # twobrid {2/W}: colored side
                colors = {p for p in parts if p in WUBRG or p == "C"}
                if colors:
                    mc.pips.append(frozenset(colors))
                else:
                    mc.generic += 2
            else:
                colors = {p for p in parts if p in WUBRG or p == "C"}
                if colors:
                    mc.pips.append(frozenset(colors))
                else:
                    mc.parse_ok = False
                    mc.note = f"hybrid?{su}"
        else:
            mc.parse_ok = False
            mc.note = f"symbol?{su}"
    return mc


def _bipartite_match(pips: list[frozenset[str]], sources: list[frozenset[str]]) -> Optional[list[int]]:
    """Max-cardinality matching of pips to mana sources. None if not all matched.

    Kuhn's algorithm. Menus here have <= ~10 sources so this is trivially fast
    and, unlike a greedy assignment, is exact -- important because a greedy
    color assignment is a classic source of "nearly right" menus.
    """
    match_src: dict[int, int] = {}

    def try_assign(pi: int, seen: set[int]) -> bool:
        for si, prod in enumerate(sources):
            if si in seen:
                continue
            if not (pips[pi] & prod):
                continue
            seen.add(si)
            if si not in match_src or try_assign(match_src[si], seen):
                match_src[si] = pi
                return True
        return False

    for pi in range(len(pips)):
        if not try_assign(pi, set()):
            return None
    used = sorted(match_src.keys())
    return used


def is_affordable(cost: ManaCost, sources: list[frozenset[str]]) -> bool:
    """Can `sources` (one mana each, all untapped) pay `cost`?"""
    if not cost.parse_ok:
        return False
    if cost.total > len(sources):
        return False
    used = _bipartite_match(cost.pips, sources)
    if used is None:
        return False
    return (len(sources) - len(used)) >= cost.generic


def land_sources(resolver: CardResolver, land_grp_ids: Iterable[int]) -> tuple[list[frozenset[str]], int]:
    """Mana sources from lands in play. Returns (sources, n_unresolved)."""
    sources: list[frozenset[str]] = []
    unresolved = 0
    for gid in land_grp_ids:
        card = resolver.get(gid)
        produced = {c.upper() for c in card.produced_mana if c.upper() in (*WUBRG, "C")}
        if produced:
            sources.append(frozenset(produced))
        elif card.resolved and card.is_land:
            sources.append(frozenset({"C"}))  # land that produces nothing known
        else:
            unresolved += 1
            sources.append(frozenset({"C"}))  # assume colorless rather than drop
    return sources, unresolved


# ---------------------------------------------------------------------------
# 17lands row access
# ---------------------------------------------------------------------------


def split_ids(value: str) -> list[int]:
    if not value:
        return []
    out = []
    for part in value.split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(float(part)))
        except ValueError:
            continue
    return out


class RowView:
    """Column-index-based accessor. DictReader on 2,579 columns is too slow."""

    def __init__(self, header: list[str]):
        self.idx = {name: i for i, name in enumerate(header)}

    def get(self, row: list[str], col: str, default: str = "") -> str:
        i = self.idx.get(col)
        if i is None or i >= len(row):
            return default
        return row[i]

    def ids(self, row: list[str], col: str) -> list[int]:
        return split_ids(self.get(row, col))

    def max_user_turn(self) -> int:
        best = 0
        for name in self.idx:
            m = re.match(r"^user_turn_(\d+)_lands_played$", name)
            if m:
                best = max(best, int(m.group(1)))
        return best


# ---------------------------------------------------------------------------
# Menu synthesis
# ---------------------------------------------------------------------------

POLICIES = ("strict", "postland", "nofilter")


@dataclass
class SynthResult:
    turn: int
    hand: list[int]
    start_lands: list[int]
    board: list[int]
    menus: dict[str, list[tuple[str, Optional[int]]]]
    picks: list[tuple[str, Optional[int]]]
    ability_picks: list[int]
    ability_menu: list[int]
    n_unresolved_hand: int
    n_uncostable_hand: int
    n_unresolved_lands: int
    n_unresolved_board: int
    x_spells: int
    multiface: int
    mana_available: int


def synthesize_turn(
    resolver: CardResolver, rv: RowView, row: list[str], turn: int, emit_abilities: bool = True
) -> Optional[SynthResult]:
    """Reconstruct the menu at the top of the user's main phase 1 on user-turn N."""
    prefix = f"user_turn_{turn}_"
    prev = f"user_turn_{turn - 1}_"

    lands_played = rv.ids(row, prefix + "lands_played")
    creatures_cast = rv.ids(row, prefix + "creatures_cast")
    non_creatures_cast = rv.ids(row, prefix + "non_creatures_cast")
    eot_hand = rv.get(row, prefix + "eot_user_cards_in_hand")
    if not (lands_played or creatures_cast or non_creatures_cast or eot_hand):
        return None  # turn not played (game ended earlier)

    # -- hand at the start of the turn --
    if turn == 1:
        base_hand = rv.ids(row, "opening_hand")
    else:
        base_hand = rv.ids(row, prev + "eot_user_cards_in_hand")
    drawn = rv.ids(row, prefix + "cards_drawn")
    # Tutored cards arrive mid-turn, so including them is optimistic for a
    # top-of-main-phase menu. Included anyway: excluding them guarantees a
    # false "played card not in menu" whenever a tutor is cast and the
    # fetched card is played the same turn.
    tutored = rv.ids(row, prefix + "cards_tutored")
    hand = base_hand + drawn + tutored

    # -- mana at the start of the turn: everything untapped after untap step --
    start_lands = rv.ids(row, prev + "eot_user_lands_in_play") if turn > 1 else []
    sources, n_unres_lands = land_sources(resolver, start_lands)

    # -- board --
    board = (
        (
            rv.ids(row, prev + "eot_user_creatures_in_play")
            + rv.ids(row, prev + "eot_user_non_creatures_in_play")
        )
        if turn > 1
        else []
    )
    n_unres_board = sum(1 for g in board if not resolver.get(g).resolved)

    # -- land drop available? 17lands records at most one land per turn --
    hand_lands = [g for g in hand if resolver.get(g).has_land_face]
    postland_sources = list(sources)
    if hand_lands:
        # "best" land = the one adding the most color flexibility
        best = max(hand_lands, key=lambda g: len(resolver.get(g).produced_mana or ()))
        extra, _ = land_sources(resolver, [best])
        postland_sources += extra

    menus: dict[str, list[tuple[str, Optional[int]]]] = {p: [] for p in POLICIES}
    n_unresolved = 0
    n_uncostable = 0
    x_spells = 0
    multiface = 0
    seen: set[int] = set()
    for gid in hand:
        card = resolver.get(gid)
        if gid not in seen:
            seen.add(gid)
            if not card.resolved:
                n_unresolved += 1
        if card.has_land_face:
            for p in POLICIES:
                menus[p].append(("Play", gid))
            if not card.is_land:  # MDFC: land back, spell front
                multiface += 1
        costs = card.castable_costs
        if not costs:
            continue
        if len(costs) > 1 or "//" in (card.mana_cost or ""):
            multiface += 1
        parsed = []
        for c in costs:
            for piece in c.split("//") if "//" in c else [c]:
                parsed.append(parse_mana_cost(piece))
        if not any(p.parse_ok for p in parsed):
            n_uncostable += 1
            # Unknown cost -> only the no-filter policy can list it.
            menus["nofilter"].append(("Cast", gid))
            continue
        if any(p.has_x for p in parsed):
            x_spells += 1
        menus["nofilter"].append(("Cast", gid))
        if any(p.parse_ok and is_affordable(p, sources) for p in parsed):
            menus["strict"].append(("Cast", gid))
        if any(p.parse_ok and is_affordable(p, postland_sources) for p in parsed):
            menus["postland"].append(("Cast", gid))

    # -- activated abilities on the battlefield (best effort) --
    ability_menu: list[int] = []
    if emit_abilities:
        for gid in dict.fromkeys(board):
            for aid in resolver.card_ability_ids(gid):
                text = resolver.ability_text(aid)
                if ":" in text and not text.lower().startswith(("whenever", "when ", "at the")):
                    ability_menu.append(aid)
        ability_menu = list(dict.fromkeys(ability_menu))

    for p in POLICIES:
        menus[p].append(("Pass", None))
        menus[p] = list(dict.fromkeys(menus[p]))

    picks: list[tuple[str, Optional[int]]] = []
    for gid in lands_played:
        picks.append(("Play", gid))
    for gid in creatures_cast + non_creatures_cast:
        picks.append(("Cast", gid))

    return SynthResult(
        turn=turn,
        hand=hand,
        start_lands=start_lands,
        board=board,
        menus=menus,
        picks=picks,
        ability_picks=rv.ids(row, prefix + "user_abilities"),
        ability_menu=ability_menu,
        n_unresolved_hand=n_unresolved,
        n_uncostable_hand=n_uncostable,
        n_unresolved_lands=n_unres_lands,
        n_unresolved_board=n_unres_board,
        x_spells=x_spells,
        multiface=multiface,
        mana_available=len(sources),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def iter_rows(path: Path, min_rank: str, max_rows: int) -> Iterator[tuple[RowView, list[str]]]:
    csv.field_size_limit(10**7)
    allowed = set(RANK_ORDER[RANK_ORDER.index(min_rank) :]) if min_rank else None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as fh:  # type: ignore[operator]
        reader = csv.reader(fh)
        header = next(reader)
        rv = RowView(header)
        rank_i = rv.idx.get("rank")
        n = 0
        for row in reader:
            if allowed is not None and rank_i is not None:
                if (row[rank_i] if rank_i < len(row) else "") not in allowed:
                    continue
            yield rv, row
            n += 1
            if n >= max_rows:
                return


def real_menu_reference(path: Path) -> dict:
    """Summarize REAL MTGA main-phase-1 menus for side-by-side comparison.

    Input is the JSONL produced by the replay-side extractor: one record per
    own-Main1 `ActionsAvailableReq` with `menu` (normalized (type, grpId)),
    `cast_costs`, `n_lands_untapped` and `pick`.
    """
    tot: list[int] = []
    comparable: list[int] = []
    casts: list[int] = []
    plays: list[int] = []
    acts: list[int] = []
    mana_acts: list[int] = []
    unaffordable = 0
    cast_entries = 0
    pick_types: Counter = Counter()
    pick_in_menu = 0
    pick_n = 0
    with path.open() as fh:
        for line in fh:
            r = json.loads(line)
            distinct = {tuple(x) for x in r["menu"]}
            tot.append(len(distinct))
            comparable.append(
                len({x for x in distinct if x[0] in ("Cast", "Play", "PlayMDFC", "Special", "Pass")})
            )
            casts.append(len({x for x in distinct if x[0] == "Cast"}))
            plays.append(len({x for x in distinct if x[0] in ("Play", "PlayMDFC")}))
            acts.append(len({x for x in distinct if x[0] == "Activate"}))
            mana_acts.append(len({x for x in distinct if x[0] in ("Activate_Mana", "FloatMana")}))
            untapped = r.get("n_lands_untapped", 0)
            for c in r.get("cast_costs") or []:
                cast_entries += 1
                if c > untapped:
                    unaffordable += 1
            picks = [tuple(x) for x in (r.get("pick") or [])]
            if picks:
                pick_n += 1
                pick_types[picks[0][0]] += 1
                if picks[0] in distinct:
                    pick_in_menu += 1

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    return {
        "decisions": len(tot),
        "mean_distinct_menu_size": mean(tot),
        "median_distinct_menu_size": statistics.median(tot) if tot else 0,
        "mean_comparable_menu_size_cast_play_pass": mean(comparable),
        "mean_cast_entries": mean(casts),
        "mean_play_entries": mean(plays),
        "mean_activate_entries": mean(acts),
        "mean_mana_ability_entries": mean(mana_acts),
        "active_cast_entries": cast_entries,
        "active_cast_entries_unaffordable_with_untapped_lands": unaffordable,
        "active_cast_entries_unaffordable_pct": round(100.0 * unaffordable / cast_entries, 2)
        if cast_entries
        else 0,
        "pick_type_mix": pick_types.most_common(),
        "pick_in_menu_pct": round(100.0 * pick_in_menu / pick_n, 2) if pick_n else None,
    }


def synth_from_zones(
    resolver: CardResolver, hand: list[int], untapped_lands: list[int], policy: str, allow_play: bool = True
) -> set[tuple[str, Optional[int]]]:
    """The menu model, applied to explicit zone contents.

    Shared by the 17lands path and the perfect-input fidelity harness so both
    measure the SAME model.
    """
    sources, _ = land_sources(resolver, untapped_lands)
    menu: list[tuple[str, Optional[int]]] = []
    for gid in dict.fromkeys(hand):
        card = resolver.get(gid)
        if card.has_land_face and allow_play:
            menu.append(("Play", gid))
        costs = card.castable_costs
        if not costs:
            continue
        parsed = [parse_mana_cost(piece) for c in costs for piece in (c.split("//") if "//" in c else [c])]
        if policy == "nofilter":
            menu.append(("Cast", gid))
            continue
        if any(p.parse_ok and is_affordable(p, sources) for p in parsed):
            menu.append(("Cast", gid))
    menu.append(("Pass", None))
    return set(menu)


def fidelity_vs_real(resolver: CardResolver, real_menus: Path) -> dict:
    """Perfect-input fidelity: real hand + real untapped lands -> real menu.

    Answers "how wrong is the MENU MODEL itself", with zero 17lands
    reconstruction error mixed in. Compared against the subset of the real
    menu a hand-only model could possibly emit (Cast / Play / Special / Pass);
    mana abilities and activated abilities are excluded from the target
    because no 17lands-derived model can produce them.
    """
    rows = [json.loads(line) for line in real_menus.open()]
    subset_landdrop = [r for r in rows if any(x[0] in ("Play", "PlayMDFC") for x in r["menu"])]
    out: dict = {"decisions": len(rows), "land_drop_available_decisions": len(subset_landdrop)}
    for label, data, allow_play in (
        ("all_decisions", rows, True),
        ("land_drop_available_only", subset_landdrop, True),
        ("cast_and_pass_only", rows, False),
    ):
        block = {}
        for policy in ("strict", "nofilter"):
            precisions, recalls, jaccards = [], [], []
            exact = 0
            fp: Counter = Counter()
            fn: Counter = Counter()
            for r in data:
                hand = [g for g in r["hand_grp_ids"] if g is not None]
                lands = [g for g in r["untapped_land_grp_ids"] if g is not None]
                real = {
                    tuple(x) for x in r["menu"] if x[0] in ("Cast", "Play", "PlayMDFC", "Special", "Pass")
                }
                real = {("Play", g) if t == "PlayMDFC" else (t, g) for t, g in real}
                if not allow_play:
                    real = {x for x in real if x[0] != "Play"}
                syn = synth_from_zones(resolver, hand, lands, policy, allow_play)
                inter = syn & real
                precisions.append(len(inter) / len(syn) if syn else 0.0)
                recalls.append(len(inter) / len(real) if real else 0.0)
                jaccards.append(len(inter) / len(syn | real) if (syn | real) else 0.0)
                if syn == real:
                    exact += 1
                for x in syn - real:
                    fp[x[0]] += 1
                for x in real - syn:
                    fn[x[0]] += 1

            def mean(xs):
                return round(sum(xs) / len(xs), 4) if xs else 0.0

            block[policy] = {
                "precision": mean(precisions),
                "recall": mean(recalls),
                "jaccard": mean(jaccards),
                "exact_menu_match_pct": round(100.0 * exact / len(data), 2) if data else 0,
                "false_positive_entries_by_type": fp.most_common(),
                "false_negative_entries_by_type": fn.most_common(),
            }
        out[label] = block
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay-csv", type=Path)
    ap.add_argument(
        "--min-rank",
        default="diamond",
        choices=[*RANK_ORDER, ""],
        help="keep rows at this rank or above (default: diamond)",
    )
    ap.add_argument("--max-rows", type=int, default=2000)
    ap.add_argument("--max-turn", type=int, default=20)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--stats-out", type=Path, default=None)
    ap.add_argument(
        "--fidelity-only",
        action="store_true",
        help="skip the 17lands pass; only run the perfect-input fidelity harness against --real-menus",
    )
    ap.add_argument("--no-abilities", action="store_true")
    ap.add_argument(
        "--real-menus",
        type=Path,
        default=None,
        help="JSONL of REAL MTGA own-Main1 menus extracted from .rply "
        "replays; adds a side-by-side reference block to the stats",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )

    t0 = time.time()
    resolver = CardResolver()

    if args.fidelity_only:
        if not args.real_menus:
            ap.error("--fidelity-only requires --real-menus")
        stats = {
            "menu_model_fidelity_perfect_inputs": fidelity_vs_real(resolver, args.real_menus),
            "real_mtga_reference": real_menu_reference(args.real_menus),
        }
        resolver.save()
        text = json.dumps(stats, indent=2)
        print(text)
        if args.stats_out:
            args.stats_out.parent.mkdir(parents=True, exist_ok=True)
            args.stats_out.write_text(text)
        return 0

    if not args.replay_csv or not args.out:
        ap.error("--replay-csv and --out are required unless --fidelity-only")

    rows_processed = 0
    menus_produced = 0
    turns_skipped_no_data = 0

    menu_sizes: dict[str, list[int]] = {p: [] for p in POLICIES}
    pick_total = 0
    pick_miss: dict[str, int] = {p: 0 for p in POLICIES}
    pick_miss_by_type: dict[str, Counter] = {p: Counter() for p in POLICIES}
    pick_not_in_hand = 0
    pick_total_by_type: Counter = Counter()

    hand_cards_seen = 0
    hand_cards_unresolved = 0
    hand_cards_uncostable = 0
    land_slots_seen = 0
    land_slots_unresolved = 0
    board_seen = 0
    board_unresolved = 0
    x_spells = 0
    multiface = 0

    ability_picks_total = 0
    ability_picks_in_menu = 0
    ability_picks_triggered_only = 0
    ability_menu_sizes: list[int] = []

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for rv, row in iter_rows(args.replay_csv, args.min_rank, args.max_rows):
            rows_processed += 1
            max_turn = min(args.max_turn, rv.max_user_turn())
            for turn in range(1, max_turn + 1):
                res = synthesize_turn(resolver, rv, row, turn, emit_abilities=not args.no_abilities)
                if res is None:
                    turns_skipped_no_data += 1
                    continue
                menus_produced += 1

                hand_distinct = set(res.hand)
                hand_cards_seen += len(hand_distinct)
                hand_cards_unresolved += res.n_unresolved_hand
                hand_cards_uncostable += res.n_uncostable_hand
                land_slots_seen += len(res.start_lands)
                land_slots_unresolved += res.n_unresolved_lands
                board_seen += len(res.board)
                board_unresolved += res.n_unresolved_board
                x_spells += res.x_spells
                multiface += res.multiface

                for p in POLICIES:
                    menu_sizes[p].append(len(res.menus[p]))

                menu_sets = {p: set(res.menus[p]) for p in POLICIES}
                for pick in res.picks:
                    pick_total += 1
                    pick_total_by_type[pick[0]] += 1
                    if pick[1] not in hand_distinct:
                        pick_not_in_hand += 1
                    for p in POLICIES:
                        if pick not in menu_sets[p]:
                            pick_miss[p] += 1
                            pick_miss_by_type[p][pick[0]] += 1

                ability_menu_sizes.append(len(res.ability_menu))
                amenu = set(res.ability_menu)
                for aid in res.ability_picks:
                    ability_picks_total += 1
                    if aid in amenu:
                        ability_picks_in_menu += 1
                    text = resolver.ability_text(aid)
                    if ":" not in text:
                        ability_picks_triggered_only += 1

                fh.write(
                    json.dumps(
                        {
                            "row": rows_processed,
                            "turn": res.turn,
                            "mana_available": res.mana_available,
                            "menu": [[t, g] for t, g in res.menus["strict"]],
                            "menu_postland": [[t, g] for t, g in res.menus["postland"]],
                            "menu_nofilter": [[t, g] for t, g in res.menus["nofilter"]],
                            "ability_menu": res.ability_menu,
                            "pick": [[t, g] for t, g in res.picks],
                            "ability_picks": res.ability_picks,
                        }
                    )
                    + "\n"
                )

    resolver.save()

    def mean(xs: list[int]) -> float:
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    stats = {
        "replay_csv": str(args.replay_csv),
        "min_rank": args.min_rank,
        "rows_processed": rows_processed,
        "menus_produced": menus_produced,
        "turn_slots_with_no_data": turns_skipped_no_data,
        "elapsed_sec": round(time.time() - t0, 1),
        "mean_menu_size": {p: mean(menu_sizes[p]) for p in POLICIES},
        "median_menu_size": {p: (statistics.median(menu_sizes[p]) if menu_sizes[p] else 0) for p in POLICIES},
        "card_resolution": {
            "distinct_hand_card_slots": hand_cards_seen,
            "hand_cards_unresolved": hand_cards_unresolved,
            "hand_cards_unresolved_pct": round(100.0 * hand_cards_unresolved / hand_cards_seen, 3)
            if hand_cards_seen
            else 0,
            "hand_cards_cost_unparseable": hand_cards_uncostable,
            "hand_cards_cost_unparseable_pct": round(100.0 * hand_cards_uncostable / hand_cards_seen, 3)
            if hand_cards_seen
            else 0,
            "land_slots": land_slots_seen,
            "land_slots_unresolved": land_slots_unresolved,
            "land_slots_unresolved_pct": round(100.0 * land_slots_unresolved / land_slots_seen, 3)
            if land_slots_seen
            else 0,
            "board_slots": board_seen,
            "board_slots_unresolved": board_unresolved,
            "board_slots_unresolved_pct": round(100.0 * board_unresolved / board_seen, 3)
            if board_seen
            else 0,
            "x_spell_menu_entries": x_spells,
            "multiface_menu_entries": multiface,
        },
        "self_consistency": {
            "actions_actually_taken": pick_total,
            "actions_by_type": pick_total_by_type.most_common(),
            "played_card_not_in_reconstructed_hand": pick_not_in_hand,
            "played_card_not_in_reconstructed_hand_pct": round(100.0 * pick_not_in_hand / pick_total, 2)
            if pick_total
            else 0,
            "played_action_missing_from_menu": {p: pick_miss[p] for p in POLICIES},
            "played_action_missing_from_menu_pct": {
                p: round(100.0 * pick_miss[p] / pick_total, 2) if pick_total else 0 for p in POLICIES
            },
            "missing_by_action_type": {p: pick_miss_by_type[p].most_common() for p in POLICIES},
        },
        "abilities": {
            "mean_synthesized_ability_menu_size": mean(ability_menu_sizes),
            "ability_events_in_17lands": ability_picks_total,
            "ability_events_in_synth_menu": ability_picks_in_menu,
            "ability_events_that_are_not_activated_abilities": ability_picks_triggered_only,
            "ability_events_not_activated_pct": round(
                100.0 * ability_picks_triggered_only / ability_picks_total, 2
            )
            if ability_picks_total
            else 0,
        },
    }

    if args.real_menus and args.real_menus.exists():
        stats["real_mtga_reference"] = real_menu_reference(args.real_menus)
        stats["menu_model_fidelity_perfect_inputs"] = fidelity_vs_real(resolver, args.real_menus)

    text = json.dumps(stats, indent=2)
    print(text)
    if args.stats_out:
        args.stats_out.parent.mkdir(parents=True, exist_ok=True)
        args.stats_out.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
