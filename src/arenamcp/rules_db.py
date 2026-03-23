"""RAG query engine for MTG Comprehensive Rules using SQLite FTS5.

Builds and queries a full-text search database over curated MTG rules,
injecting relevant rules into LLM coaching prompts to ground advice in
official game rules.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".arenamcp" / "cache"
DB_PATH = CACHE_DIR / "rules.db"

# Trigger → base search queries mapping
_TRIGGER_QUERIES = {
    "combat_attackers": [
        "attack declare attackers",
        "summoning sickness haste",
        "vigilance tap attacking",
    ],
    "combat_blockers": [
        "block declare blockers untapped",
        "flying reach block",
        "menace block two creatures",
        "deathtouch lethal damage",
    ],
    "new_turn": [
        "main phase sorcery",
        "land play turn",
    ],
    "stack_spell_opponent": [
        "stack priority respond",
        "counter spell",
    ],
    "stack_spell_yours": [
        "stack priority resolve",
    ],
    "stack_spell": [
        "stack priority resolve respond",
    ],
    "low_life": [
        "life damage lethal",
        "lifelink damage",
    ],
    "opponent_low_life": [
        "life damage lethal",
        "trample damage excess",
    ],
    "decision_required": [
        "scry library top bottom",
        "surveil graveyard",
        "discard hand",
    ],
    "spell_resolved": [
        "resolve enters battlefield",
        "triggered ability",
    ],
    "priority_gained": [
        "priority cast activate",
        "instant ability",
    ],
    "threat_detected": [
        "enters battlefield triggered",
        "priority respond",
    ],
}

# Keywords to scan for on battlefield cards
_KEYWORD_SEARCH_TERMS = {
    "flying": "flying block",
    "reach": "reach block flying",
    "first strike": "first strike combat damage",
    "double strike": "double strike combat damage",
    "deathtouch": "deathtouch lethal damage",
    "trample": "trample excess damage",
    "lifelink": "lifelink damage life gain",
    "vigilance": "vigilance tap attacking",
    "haste": "haste summoning sickness",
    "flash": "flash instant cast",
    "hexproof": "hexproof target",
    "ward": "ward target counter",
    "indestructible": "indestructible destroy lethal",
    "menace": "menace block two",
    "defender": "defender attack",
    "protection": "protection block target damage",
    "prowess": "prowess noncreature spell",
    "toxic": "toxic poison counters",
    "skulk": "skulk block power",
}


class RulesDB:
    """SQLite FTS5 database for querying MTG Comprehensive Rules."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Ensure the database exists and return a connection."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        needs_build = not self._db_path.exists()
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row

        if needs_build:
            self._build_db()
        else:
            # Verify table exists (may have been corrupted/empty)
            cursor = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='rules'"
            )
            if cursor.fetchone() is None:
                self._build_db()

        return self._conn

    def _build_db(self) -> None:
        """Build the FTS5 database from the rules corpus."""
        from arenamcp.rules_data import RULES

        conn = self._conn
        conn.execute("DROP TABLE IF EXISTS rules")
        conn.execute(
            """CREATE VIRTUAL TABLE rules USING fts5(
                number,
                section,
                category,
                text,
                tokenize='porter unicode61'
            )"""
        )
        conn.executemany(
            "INSERT INTO rules (number, section, category, text) VALUES (?, ?, ?, ?)",
            [(r["number"], r["section"], r["category"], r["text"]) for r in RULES],
        )
        conn.commit()
        logger.info(f"Built rules DB with {len(RULES)} rules at {self._db_path}")

    def rebuild(self) -> None:
        """Force rebuild the database (e.g. after rules_data update)."""
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._db_path.exists():
            self._db_path.unlink()
        self._ensure_db()

    def query(
        self,
        search_terms: str,
        category: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Query rules using FTS5 full-text search.

        Args:
            search_terms: Space-separated search terms (FTS5 MATCH syntax)
            category: Optional category filter (e.g. "combat", "keywords")
            limit: Max results to return

        Returns:
            List of {number, section, text} dicts ordered by relevance
        """
        conn = self._ensure_db()

        # Clean search terms for FTS5 - use OR between words for broader matching
        words = search_terms.split()
        if not words:
            return []

        # Build FTS5 query: each word as OR for broader matching
        fts_query = " OR ".join(words)

        try:
            if category:
                cursor = conn.execute(
                    """SELECT number, section, text, rank
                       FROM rules
                       WHERE rules MATCH ? AND category = ?
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_query, category, limit),
                )
            else:
                cursor = conn.execute(
                    """SELECT number, section, text, rank
                       FROM rules
                       WHERE rules MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_query, limit),
                )

            return [
                {"number": row["number"], "section": row["section"], "text": row["text"]}
                for row in cursor.fetchall()
            ]
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 query error for '{search_terms}': {e}")
            return []

    def get_rules_for_situation(
        self,
        game_state: dict,
        trigger: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Get rules relevant to the current game situation.

        This is the main entry point for coach.py. Extracts keywords from
        the game state and trigger, runs targeted FTS5 queries, deduplicates,
        and returns the top N most relevant rules.

        Args:
            game_state: Dict from get_game_state()
            trigger: Trigger name (e.g. "combat_blockers", "new_turn")
            limit: Max rules to return (default 5)

        Returns:
            List of {number, section, text} dicts
        """
        queries = self._extract_situation_keywords(game_state, trigger)
        if not queries:
            return []

        # Run all queries and collect results
        seen_numbers: set[str] = set()
        results: list[dict] = []

        for search_terms in queries:
            for rule in self.query(search_terms, limit=3):
                if rule["number"] not in seen_numbers:
                    seen_numbers.add(rule["number"])
                    results.append(rule)

        # Return top N by insertion order (earlier queries = higher priority)
        return results[:limit]

    def _extract_situation_keywords(
        self,
        game_state: dict,
        trigger: Optional[str],
    ) -> list[str]:
        """Extract search queries from game state and trigger.

        Maps the trigger to base search terms, then scans the battlefield
        for keyword abilities to add context-specific queries.

        Returns:
            List of FTS5 search query strings
        """
        queries: list[str] = []

        # 1. Base queries from trigger
        if trigger and trigger in _TRIGGER_QUERIES:
            queries.extend(_TRIGGER_QUERIES[trigger])

        # 2. Scan battlefield cards for keyword abilities
        battlefield = game_state.get("battlefield", [])
        found_keywords: set[str] = set()

        for card in battlefield:
            oracle = card.get("oracle_text", "").lower()
            type_line = card.get("type_line", "").lower()
            # Also check keywords list if available
            keywords_list = card.get("keywords", [])
            if isinstance(keywords_list, list):
                for kw in keywords_list:
                    if isinstance(kw, str):
                        found_keywords.add(kw.lower())

            # Scan oracle text for keywords
            for keyword in _KEYWORD_SEARCH_TERMS:
                if keyword in oracle or keyword in type_line:
                    found_keywords.add(keyword)

        # Add keyword-specific queries
        for keyword in found_keywords:
            if keyword in _KEYWORD_SEARCH_TERMS:
                queries.append(_KEYWORD_SEARCH_TERMS[keyword])

        # 3. Scan stack for spell types
        stack = game_state.get("stack", [])
        if stack:
            queries.append("stack resolve priority")
            for item in stack:
                type_line = item.get("type_line", "").lower()
                if "instant" in type_line:
                    queries.append("instant priority respond")
                elif "sorcery" in type_line:
                    queries.append("sorcery main phase")

        # 4. Check for specific game situations
        players = game_state.get("players", [])
        for p in players:
            life = p.get("life_total", 20)
            if isinstance(life, (int, float)) and life <= 5:
                queries.append("life damage lethal")
                break

        return queries
