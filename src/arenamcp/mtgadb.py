"""MTGA local card database reader using SQLite.

Reads card data directly from MTGA's installed CardDatabase SQLite file,
providing complete coverage of all Arena cards including tokens and
special versions that Scryfall may not have arena_id mappings for.
"""

import glob
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MTGACard:
    """Card data from MTGA's local database."""
    grp_id: int
    name: str
    types: str
    power: str
    toughness: str
    colors: str
    is_token: bool
    expansion_code: str
    oracle_text: str = ""


# Common MTGA installation paths
MTGA_PATHS = [
    # Steam
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Steam/steamapps/common/MTGA",
    # Epic Games
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Epic Games/MagicTheGathering",
    # Standalone installer
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Wizards of the Coast/MTGA",
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Wizards of the Coast/MTGA",
]


def find_mtga_database() -> Optional[Path]:
    """Find the most recent MTGA CardDatabase SQLite file.

    Searches common installation paths and returns the newest database file found.
    This handles cases where multiple installations exist (some stale) or 
    multiple DB versions exist in the folder.

    Returns:
        Path to the database file, or None if not found.
    """
    candidates = []
    
    for base_path in MTGA_PATHS:
        raw_dir = base_path / "MTGA_Data" / "Downloads" / "Raw"
        if raw_dir.exists():
            # Find the CardDatabase file (name includes hash)
            pattern = str(raw_dir / "Raw_CardDatabase_*.mtga")
            matches = glob.glob(pattern)
            for match in matches:
                p = Path(match)
                try:
                    candidates.append((p.stat().st_mtime, p))
                except Exception:
                    pass

    if not candidates:
        logger.warning("MTGA CardDatabase not found in common locations")
        return None
        
    # Sort by modification time descending (newest first)
    candidates.sort(key=lambda x: x[0], reverse=True)
    
    best_path = candidates[0][1]
    logger.info(f"Found MTGA database: {best_path} (timestamp: {candidates[0][0]})")
    return best_path


class MTGADatabase:
    """Reader for MTGA's local CardDatabase SQLite file.

    Provides fast lookups by GrpId (arena_id) with complete coverage
    of all Arena cards.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the database reader.

        Args:
            db_path: Path to CardDatabase file. Auto-detects if not provided.
        """
        self._db_path = db_path or find_mtga_database()
        self._conn: Optional[sqlite3.Connection] = None
        self._available = False
        self._error_count = 0  # Track consecutive errors for reconnection

        self._connect()

    def _connect(self) -> None:
        """Open a read-only SQLite connection to the MTGA database."""
        self._conn = None
        self._available = False
        self._error_count = 0

        if not self._db_path or not self._db_path.exists():
            logger.warning("MTGA database not available")
            return

        try:
            # Use non-URI mode to avoid path encoding issues on Windows
            # (spaces, backslashes in "C:\Program Files (x86)\..." break URI mode)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False
            )
            # Open as read-only via PRAGMA
            self._conn.execute("PRAGMA query_only = true;")
            self._conn.execute("PRAGMA read_uncommitted = true;")
            self._conn.row_factory = sqlite3.Row
            self._available = True
            logger.info("MTGA database connected")
        except Exception as e:
            logger.error(f"Failed to open MTGA database: {e}")

    @property
    def available(self) -> bool:
        """Check if the database is available."""
        return self._available

    def _resolve_oracle_text(self, ability_ids_str: Optional[str]) -> str:
        """Resolve full oracle text from AbilityIds string.

        Args:
            ability_ids_str: Comma-separated 'AbilityGrpId:TextId' string.

        Returns:
            Combined oracle text for the card.
        """
        if not ability_ids_str:
            return ""

        try:
            # Parse 'AbilityGrpId:TextId' pairs — convert to integers,
            # skipping malformed entries that would cause SQLite MISUSE errors.
            text_ids: list[int] = []
            for pair in ability_ids_str.split(','):
                parts = pair.split(':')
                if len(parts) >= 2:
                    raw = parts[1].strip()
                    if raw:
                        try:
                            text_ids.append(int(raw))
                        except ValueError:
                            continue

            if not text_ids:
                return ""

            # Query all text IDs at once, preserving original order
            placeholders = ",".join("?" * len(text_ids))
            # Embed integer IDs directly in CASE WHEN — SQLite doesn't support
            # parameterized ? placeholders in CASE expressions (causes MISUSE error).
            # Safe because text_ids are validated ints parsed on line 159 above.
            case_whens = " ".join(
                f"WHEN {tid} THEN {i}" for i, tid in enumerate(text_ids)
            )
            cursor = self._conn.execute(f"""
                SELECT Loc FROM Localizations_enUS
                WHERE LocId IN ({placeholders})
                ORDER BY CASE LocId
                    {case_whens}
                END
            """, text_ids)

            texts = [row["Loc"] for row in cursor.fetchall() if row["Loc"]]
            return "\n".join(texts)

        except Exception as e:
            logger.warning(f"Failed to resolve oracle text: {e}")
            return ""

    def get_card(self, grp_id: int) -> Optional[MTGACard]:
        """Look up a card by GrpId (arena_id).

        Args:
            grp_id: The MTGA GrpId / arena_id

        Returns:
            MTGACard with card data, or None if not found.
        """
        if not self._available or not self._conn:
            return None

        try:
            cursor = self._conn.execute("""
                SELECT
                    c.GrpId,
                    l.Loc as Name,
                    c.Types,
                    c.Power,
                    c.Toughness,
                    c.Colors,
                    c.IsToken,
                    c.ExpansionCode,
                    c.AbilityIds,
                    c.Order_Title
                FROM Cards c
                LEFT JOIN Localizations_enUS l ON c.TitleId = l.LocId AND l.Formatted = 1
                WHERE c.GrpId = ?
            """, (int(grp_id),))

            row = cursor.fetchone()
            if row:
                oracle_text = self._resolve_oracle_text(row["AbilityIds"])
                name = row["Name"] or row["Order_Title"] or f"Unknown_({grp_id})"
                # Strip HTML tags that MTGA injects into hyphenated names
                if "<" in name:
                    import re
                    name = re.sub(r"<[^>]+>", "", name)

                self._error_count = 0
                return MTGACard(
                    grp_id=row["GrpId"],
                    name=name,
                    types=row["Types"],
                    power=row["Power"] or "",
                    toughness=row["Toughness"] or "",
                    colors=row["Colors"] or "",
                    is_token=bool(row["IsToken"]),
                    expansion_code=row["ExpansionCode"] or "",
                    oracle_text=oracle_text
                )
        except Exception as e:
            self._error_count += 1
            if self._error_count <= 3:
                logger.error(f"Database query error for grp_id {grp_id}: {e}")
            if self._error_count >= 5:
                logger.warning("MTGA database: too many errors, attempting reconnect")
                self._connect()

        return None

    def get_cards_batch(self, grp_ids: list[int]) -> dict[int, MTGACard]:
        """Look up multiple cards at once.

        Args:
            grp_ids: List of GrpIds to look up

        Returns:
            Dict mapping grp_id to MTGACard for found cards.
        """
        if not self._available or not self._conn or not grp_ids:
            return {}

        results = {}
        try:
            placeholders = ",".join("?" * len(grp_ids))
            cursor = self._conn.execute(f"""
                SELECT
                    c.GrpId,
                    l.Loc as Name,
                    c.Types,
                    c.Power,
                    c.Toughness,
                    c.Colors,
                    c.IsToken,
                    c.ExpansionCode,
                    c.AbilityIds,
                    c.Order_Title
                FROM Cards c
                LEFT JOIN Localizations_enUS l ON c.TitleId = l.LocId AND l.Formatted = 1
                WHERE c.GrpId IN ({placeholders})
            """, grp_ids)

            for row in cursor.fetchall():
                oracle_text = self._resolve_oracle_text(row["AbilityIds"])
                name = row["Name"] or row["Order_Title"] or f"Unknown_({row['GrpId']})"
                # Strip HTML tags that MTGA injects into hyphenated names
                if "<" in name:
                    import re
                    name = re.sub(r"<[^>]+>", "", name)

                card = MTGACard(
                    grp_id=row["GrpId"],
                    name=name,
                    types=row["Types"],
                    power=row["Power"] or "",
                    toughness=row["Toughness"] or "",
                    colors=row["Colors"] or "",
                    is_token=bool(row["IsToken"]),
                    expansion_code=row["ExpansionCode"] or "",
                    oracle_text=oracle_text
                )
                results[card.grp_id] = card
        except Exception as e:
            logger.error(f"Batch query error: {e}")

        return results

    def get_ability_text(self, ability_id: int) -> Optional[str]:
        """Look up text for an ability ID (e.g. stack object).
        
        Args:
            ability_id: The Ability Id (grp_id of the stack object).
            
        Returns:
            Review of the ability text, or None if not found.
        """
        if not self._available or not self._conn:
            return None
            
        try:
            # First get TextId from Abilities table
            cursor = self._conn.execute(
                "SELECT TextId FROM Abilities WHERE Id = ?", 
                (ability_id,)
            )
            row = cursor.fetchone()
            
            if not row:
                return None
                
            text_id = row["TextId"]
            
            # Then get text from Localizations
            cursor = self._conn.execute(
                "SELECT Loc FROM Localizations_enUS WHERE LocId = ?",
                (text_id,)
            )
            loc_row = cursor.fetchone()
            
            if loc_row:
                return loc_row["Loc"]
                
        except Exception as e:
            logger.error(f"Ability lookup error for id {ability_id}: {e}")
            
        return None

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._available = False
