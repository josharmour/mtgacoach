"""Unified card database interface with fallback chain.

Provides a common protocol for card lookups across multiple data sources
(MTGA local DB, MTGJSON, Scryfall) and a FallbackCardDatabase that tries
each source in order until a result is found.
"""

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# MTGA's ``Cards.Types`` column is a comma-separated list of numeric CardType
# enum ids ("5" = Land, "1,2" = Artifact Creature).  It used to be handed to
# callers as ``CardInfo.type_line``, which made a lookup that had produced
# nothing useful indistinguishable from a real answer: ``"land" in type_line``
# is False for "5", so a battlefield full of Plains rendered as "Mana: 0" and
# every affordability tag in the prompt was wrong.  Nothing may present one of
# these as a type line.
_NUMERIC_TYPE_CODE_RE = re.compile(r"^\s*\d+(?:\s*,\s*\d+)*\s*$")


# MTGA's WellKnownCatalogId enum (re-output/SharedClientCore/WellKnownCatalogId.cs).
# These grpIds are placeholders, not printings — MTGA itself skips printing
# lookups for them (CardData.cs: `_instance.GrpId != 3 && _instance.GrpId != 4`).
# A face-down creature is hidden *by the rules*; failing to resolve it is the
# correct outcome and must not be confused with a card database that did not
# load. Nothing here is a defect.
WELL_KNOWN_CATALOG_IDS: dict[int, str] = {
    1: "Token",
    2: "Emblem",
    3: "StandardCardBack",  # face-down / hidden card
    4: "Obscured",
}


def is_hidden_card_id(arena_id: int | None) -> bool:
    """True if this grpId is MTGA's placeholder for a card nobody can see.

    Distinguishes "the game is hiding this" from "our lookup failed", which
    are otherwise both just a missing card.
    """
    if arena_id is None:
        return False
    try:
        return int(arena_id) in (3, 4)
    except (TypeError, ValueError):
        return False


def is_bogus_type_line(type_line: str | None) -> bool:
    """True if ``type_line`` is a placeholder/encoding artefact, not a type line.

    A real type line always contains a word ("Land", "Creature — Spider").
    A bare number, or a comma-separated list of numbers, is MTGA's internal
    type-code encoding leaking through and must be treated as *unknown*
    rather than as data.
    """
    if not type_line:
        return False  # empty is honestly-unknown, handled separately
    return bool(_NUMERIC_TYPE_CODE_RE.match(type_line))


@dataclass
class CardInfo:
    """Unified card data returned by all card database implementations.

    This is the common currency for card lookups across all sources.
    Fields that a source cannot provide are left as their defaults.
    """

    name: str
    oracle_text: str = ""
    type_line: str = ""
    mana_cost: str = ""
    cmc: float = 0.0
    colors: list[str] = field(default_factory=list)
    arena_id: int = 0
    scryfall_uri: str = ""
    source: str = ""  # Which database provided this result


@runtime_checkable
class CardDatabase(Protocol):
    """Protocol for card database implementations.

    Any class implementing get_card_by_arena_id and get_card_by_name
    with the correct signatures satisfies this protocol.
    """

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        """Look up a card by MTGA arena_id / grp_id.

        Args:
            arena_id: The MTGA arena ID of the card.

        Returns:
            CardInfo if found, None otherwise.
        """
        ...

    def get_card_by_name(self, name: str) -> CardInfo | None:
        """Look up a card by name.

        Args:
            name: The card name to search for.

        Returns:
            CardInfo if found, None otherwise.
        """
        ...


class ScryfallAdapter:
    """Adapts ScryfallCache to the CardDatabase protocol."""

    def __init__(self, scryfall_cache: Any) -> None:
        self._cache = scryfall_cache

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        card = self._cache.get_card_by_arena_id(arena_id)
        if card is None:
            return None
        return CardInfo(
            name=card.name or "",
            oracle_text=card.oracle_text or "",
            type_line=card.type_line or "",
            mana_cost=card.mana_cost or "",
            cmc=card.cmc or 0.0,
            colors=card.colors or [],
            arena_id=card.arena_id or 0,
            scryfall_uri=card.scryfall_uri or "",
            source="scryfall",
        )

    def get_card_by_name(self, name: str) -> CardInfo | None:
        card = self._cache.get_card_by_name(name)
        if card is None:
            return None
        return CardInfo(
            name=card.name or "",
            oracle_text=card.oracle_text or "",
            type_line=card.type_line or "",
            mana_cost=card.mana_cost or "",
            cmc=card.cmc or 0.0,
            colors=card.colors or [],
            arena_id=card.arena_id or 0,
            scryfall_uri=card.scryfall_uri or "",
            source="scryfall",
        )


class MTGJSONAdapter:
    """Adapts MTGJSONDatabase to the CardDatabase protocol."""

    def __init__(self, mtgjson_db: Any) -> None:
        self._db = mtgjson_db

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        if not self._db.available:
            return None
        # MTGJSONDatabase uses get_card() for arena_id lookups
        card = self._db.get_card(arena_id)
        if card is None:
            return None
        return CardInfo(
            name=card.name or "",
            oracle_text=card.oracle_text or "",
            type_line=card.type_line or "",
            mana_cost=card.mana_cost or "",
            cmc=card.cmc or 0.0,
            colors=card.colors or [],
            arena_id=card.arena_id or 0,
            source="mtgjson",
        )

    def get_card_by_name(self, name: str) -> CardInfo | None:
        if not self._db.available:
            return None
        card = self._db.get_card_by_name(name)
        if card is None:
            return None
        return CardInfo(
            name=card.name or "",
            oracle_text=card.oracle_text or "",
            type_line=card.type_line or "",
            mana_cost=card.mana_cost or "",
            cmc=card.cmc or 0.0,
            colors=card.colors or [],
            arena_id=card.arena_id or 0,
            source="mtgjson",
        )


class MTGADatabaseAdapter:
    """Adapts MTGADatabase to the CardDatabase protocol.

    Also exposes MTGA-specific features (ability text, batch lookups)
    that don't fit the generic protocol.

    A miss returns ``None``.  A hit never returns a numeric type code in
    ``type_line`` — it returns MTGA's real localized type line, or "" if the
    localization is missing.  Both of those are honest; "5" was not.
    """

    def __init__(self, mtga_db: Any) -> None:
        self._db = mtga_db

    @property
    def available(self) -> bool:
        """Check if the underlying MTGA database is available."""
        return self._db.available

    @staticmethod
    def _type_line(card: Any) -> str:
        """Real localized type line, never MTGA's numeric ``Types`` codes."""
        type_line = (getattr(card, "type_line", "") or "").strip()
        if is_bogus_type_line(type_line):
            logger.debug(
                "MTGA DB gave a numeric type code (%r) for grp_id %s — reporting unknown",
                type_line,
                getattr(card, "grp_id", "?"),
            )
            return ""
        return type_line

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        if not self._db.available:
            return None
        card = self._db.get_card(arena_id)
        if card is None:
            return None
        return CardInfo(
            name=card.name or "",
            oracle_text=card.oracle_text or "",
            type_line=self._type_line(card),
            mana_cost="",  # MTGA DB doesn't store mana cost
            cmc=0.0,
            colors=card.colors.split(",") if card.colors else [],
            arena_id=card.grp_id,
            source="mtgadb",
        )

    def get_card_by_name(self, name: str) -> CardInfo | None:
        """MTGA local DB does not support name-based lookups."""
        return None

    def get_ability_text(self, ability_id: int) -> str | None:
        """Look up text for an ability ID (MTGA-specific)."""
        return self._db.get_ability_text(ability_id)

    def get_cards_batch(self, grp_ids: list[int]) -> dict[int, Any]:
        """Batch lookup (MTGA-specific). Returns raw MTGACard objects."""
        return self._db.get_cards_batch(grp_ids)

    def prewarm_cards(self, grp_ids: list[int]) -> dict[int, CardInfo]:
        """Pre-warm MTGA database card cache for GrpIds."""
        cards = self._db.prewarm_cards(grp_ids)
        res = {}
        for grp_id, card in cards.items():
            res[grp_id] = CardInfo(
                name=card.name or "",
                oracle_text=card.oracle_text or "",
                type_line=self._type_line(card),
                mana_cost="",
                cmc=0.0,
                colors=card.colors.split(",") if card.colors else [],
                arena_id=card.grp_id,
                source="mtgadb",
            )
        return res

    @property
    def raw(self) -> Any:
        """Access the underlying MTGADatabase for MTGA-specific operations."""
        return self._db


class NullCardDatabase:
    """No-op card database used when all initializations fail."""

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        return None

    def get_card_by_name(self, name: str) -> CardInfo | None:
        return None


class FallbackCardDatabase:
    """Card database that tries multiple sources in order.

    The default fallback chain is:
    1. MTGJSON (most complete oracle text, updated daily)
    2. MTGA local DB (has newest cards, tokens, digital-only)
    3. Scryfall (API fallback, rate-limited)

    For arena_id lookups, sources are tried in order. If a source returns
    a card without oracle_text, additional sources are consulted to enrich it.
    Name-based lookups follow the same pattern.
    """

    def __init__(self, sources: list[CardDatabase] | None = None) -> None:
        """Initialize with a list of CardDatabase sources.

        Args:
            sources: Ordered list of card databases to try. If None,
                     must be set later via set_sources().
        """
        self._sources: list[CardDatabase] = sources or []
        # Keep typed references for source-specific features
        self._mtga_adapter: MTGADatabaseAdapter | None = None
        for src in self._sources:
            if isinstance(src, MTGADatabaseAdapter):
                self._mtga_adapter = src
                break

    @property
    def sources(self) -> list[CardDatabase]:
        """Return the list of configured sources."""
        return list(self._sources)

    @staticmethod
    def _needs_enrichment(card: CardInfo) -> bool:
        """Check whether a card result is missing critical fields.

        The MTGA local DB returns oracle_text but not mana_cost, so a result
        sourced only from it is worth topping up from MTGJSON/Scryfall by name.
        """
        if not card.mana_cost:
            # Lands legitimately have no mana cost, but non-lands should
            card_types = getattr(card, "card_types", []) or []
            is_land = any("land" in ct.lower() for ct in card_types)
            if not is_land and card.type_line not in ("Land",):
                return True
        # A numeric type code is not a type line (defence in depth — the MTGA
        # adapter already refuses to emit one).
        if is_bogus_type_line(card.type_line):
            return True
        return bool(not card.type_line)

    def _enrich_from_name(self, card: CardInfo) -> None:
        """Fill in missing mana_cost / type_line / cmc by name lookup."""
        if not card.name or card.name.startswith("Unknown"):
            return
        name_result = self.get_card_by_name(card.name)
        if name_result is None:
            return
        if not card.oracle_text and name_result.oracle_text:
            card.oracle_text = name_result.oracle_text
        if self._needs_enrichment(card):
            # Only accept a type line that IS one. Enrichment previously took
            # any truthy value, so a source returning a numeric type code could
            # overwrite a correctly resolved type line with a meaningless one.
            if name_result.type_line and not is_bogus_type_line(name_result.type_line):
                card.type_line = name_result.type_line
            if name_result.mana_cost:
                card.mana_cost = name_result.mana_cost
            if name_result.cmc and not card.cmc:
                card.cmc = name_result.cmc
            if name_result.colors and not card.colors:
                card.colors = name_result.colors

    def get_card_by_arena_id(self, arena_id: int) -> CardInfo | None:
        """Look up a card by arena_id, trying each source in order.

        If a source returns a result without oracle_text, continues
        checking remaining sources and merges in the oracle_text.
        After all sources are tried, enriches missing fields (mana_cost,
        type_line) via name-based lookup.
        """
        best: CardInfo | None = None

        for source in self._sources:
            try:
                result = source.get_card_by_arena_id(arena_id)
            except Exception as exc:
                logger.debug(
                    "Card lookup failed in %s for arena_id %d: %s",
                    type(source).__name__,
                    arena_id,
                    exc,
                )
                continue

            if result is None:
                continue

            if best is None:
                best = result

            # If we have oracle_text AND don't need enrichment, we're done
            if best.oracle_text and not self._needs_enrichment(best):
                return best

            # Merge oracle_text from this source into best
            if result.oracle_text and not best.oracle_text:
                best.oracle_text = result.oracle_text
            # Merge other fields if they're better
            if result.type_line and (not best.type_line or self._needs_enrichment(best)):
                if not is_bogus_type_line(result.type_line):
                    best.type_line = result.type_line
            if result.mana_cost and not best.mana_cost:
                best.mana_cost = result.mana_cost
            if result.cmc and not best.cmc:
                best.cmc = result.cmc
            if result.colors and not best.colors:
                best.colors = result.colors
            if result.scryfall_uri and not best.scryfall_uri:
                best.scryfall_uri = result.scryfall_uri

        # Enrich missing fields (mana_cost, type_line) via name lookup
        if best and self._needs_enrichment(best):
            self._enrich_from_name(best)

        # Last resort: if we have a name but no oracle_text at all
        if best and not best.oracle_text and best.name and not best.name.startswith("Unknown"):
            self._enrich_from_name(best)

        return best

    def get_card_by_name(self, name: str) -> CardInfo | None:
        """Look up a card by name, trying each source in order."""
        for source in self._sources:
            try:
                result = source.get_card_by_name(name)
            except Exception as exc:
                logger.debug(
                    "Card name lookup failed in %s for '%s': %s",
                    type(source).__name__,
                    name,
                    exc,
                )
                continue

            if result is not None:
                return result

        return None

    def get_ability_text(self, ability_id: int) -> str | None:
        """Look up ability text (delegates to MTGA adapter if available)."""
        if self._mtga_adapter:
            return self._mtga_adapter.get_ability_text(ability_id)
        return None

    def prewarm_cards(self, grp_ids: list[int]) -> dict[int, CardInfo]:
        """Pre-warm card database lookups for GrpIds across sources."""
        if self._mtga_adapter:
            return self._mtga_adapter.prewarm_cards(grp_ids)
        return {}

    def get_raw_mtgadb(self) -> Any | None:
        """Get the underlying MTGADatabase for MTGA-specific operations.

        Returns None if MTGA database is not available.
        """
        if self._mtga_adapter:
            return self._mtga_adapter.raw
        return None


def create_card_database(
    scryfall_cache: Any | None = None,
    mtgjson_db: Any | None = None,
    mtga_db: Any | None = None,
) -> FallbackCardDatabase:
    """Create a FallbackCardDatabase with the standard source order.

    The default order is: MTGJSON -> MTGA local DB -> Scryfall.
    Sources that are None are skipped.

    Args:
        scryfall_cache: A ScryfallCache instance (or _NullScryfallCache).
        mtgjson_db: A MTGJSONDatabase instance.
        mtga_db: A MTGADatabase instance.

    Returns:
        Configured FallbackCardDatabase.
    """
    sources: list[CardDatabase] = []

    if mtgjson_db is not None:
        sources.append(MTGJSONAdapter(mtgjson_db))

    if mtga_db is not None:
        sources.append(MTGADatabaseAdapter(mtga_db))

    if scryfall_cache is not None:
        sources.append(ScryfallAdapter(scryfall_cache))

    return FallbackCardDatabase(sources)


# ---------------------------------------------------------------------------
# Fail-closed loader for prompt / corpus / dataset builders
# ---------------------------------------------------------------------------


class CardDatabaseUnavailable(RuntimeError):
    """A card source a builder depends on could not be loaded.

    Raised instead of returning a partially-populated database, because a
    builder that carries on produces records that *look* fine: a battlefield
    whose lands did not resolve renders "Mana: 0" and mis-tags every card's
    affordability, which is indistinguishable from a real 0-mana board.
    """


def require_card_database(*, allow_network: bool = False) -> FallbackCardDatabase:
    """A **fully loaded** card database, or an exception. Never a degraded one.

    ``get_mtgjson()`` returns immediately and loads in a background daemon
    thread, so a builder that starts rendering prompts right away resolves an
    arbitrary, timing-dependent subset of cards. That race silently corrupted
    20.4% of the on-disk play-decision gate corpus. Every prompt, corpus or
    training-data builder must obtain its database here.

    This is deliberately *not* ``get_card_database()``: that singleton is the
    live-app path, where degrading gracefully is correct because the coach
    must keep running. A corpus builder has the opposite obligation — it must
    stop.

    Args:
        allow_network: Include the Scryfall API fallback. Off by default: a
            built corpus must be reproducible without the network, and
            Scryfall rate-limits at corpus volume.

    Raises:
        CardDatabaseUnavailable: if MTGJSON or the MTGA local DB is missing.
    """
    from arenamcp.mtgadb import MTGADatabase
    from arenamcp.mtgjson import get_mtgjson

    missing: list[str] = []

    mtgjson_db = None
    try:
        mtgjson_db = get_mtgjson(block=True)
        if not mtgjson_db.available:
            missing.append("MTGJSON (load failed or cache absent)")
    except Exception as e:  # noqa: BLE001
        missing.append(f"MTGJSON ({type(e).__name__}: {e})")

    mtga_db = None
    try:
        mtga_db = MTGADatabase()
        if not mtga_db.available:
            missing.append("MTGA local CardDatabase (MTGA install not found)")
    except Exception as e:  # noqa: BLE001
        missing.append(f"MTGA local CardDatabase ({type(e).__name__}: {e})")

    if missing:
        raise CardDatabaseUnavailable(
            "refusing to build with unresolved card facts — unavailable sources: "
            + "; ".join(missing)
            + ". Records built now would carry empty type lines, so battlefield "
            "lands would produce no mana and affordability tags would be wrong."
        )

    scryfall_cache = None
    if allow_network:
        try:
            from arenamcp.scryfall import ScryfallCache

            scryfall_cache = ScryfallCache()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Scryfall fallback unavailable: {e}")

    return create_card_database(
        scryfall_cache=scryfall_cache,
        mtgjson_db=mtgjson_db,
        mtga_db=mtga_db,
    )


# ---------------------------------------------------------------------------
# Module-level lazy singleton
# ---------------------------------------------------------------------------
_card_db: FallbackCardDatabase | None = None
_card_db_lock = threading.Lock()


def get_card_database() -> FallbackCardDatabase:
    """Get or create the global FallbackCardDatabase singleton.

    Lazily initializes all three card sources on first call:
      - MTGJSON (via get_mtgjson())
      - MTGA local DB (via MTGADatabase())
      - Scryfall (via ScryfallCache(), with graceful fallback)

    Thread-safe.
    """
    global _card_db
    if _card_db is not None:
        return _card_db

    with _card_db_lock:
        if _card_db is not None:
            return _card_db

        logger.info("Initializing unified card database...")

        # -- MTGJSON --
        mtgjson_db = None
        try:
            from arenamcp.mtgjson import get_mtgjson

            mtgjson_db = get_mtgjson()
        except Exception as e:
            logger.warning(f"MTGJSON init failed: {e}")

        # -- MTGA local DB --
        mtga_db = None
        try:
            from arenamcp.mtgadb import MTGADatabase

            mtga_db = MTGADatabase()
        except Exception as e:
            logger.warning(f"MTGA database init failed: {e}")

        # -- Scryfall --
        scryfall_cache = None
        try:
            from arenamcp.scryfall import ScryfallCache

            scryfall_cache = ScryfallCache()
        except Exception as e:
            logger.warning(f"Scryfall init failed: {e}")

        _card_db = create_card_database(
            scryfall_cache=scryfall_cache,
            mtgjson_db=mtgjson_db,
            mtga_db=mtga_db,
        )
        logger.info(
            "Unified card database ready with %d sources: %s",
            len(_card_db.sources),
            [type(s).__name__ for s in _card_db.sources],
        )
        return _card_db


def set_card_database(db: FallbackCardDatabase) -> None:
    """Replace the global card database singleton (for testing)."""
    global _card_db
    _card_db = db


def reset_card_database() -> None:
    """Reset the global card database singleton (for testing)."""
    global _card_db
    _card_db = None
