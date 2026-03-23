"""NetworkX-based synergy graph for analyzing card interactions and combos.

Nodes represent cards, edges represent synergy relationships with weighted
scores. Built from Scryfall bulk data on first run, then cached to disk.
"""

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Any, Optional

import networkx as nx

logger = logging.getLogger(__name__)

# Cache paths
CACHE_DIR = Path.home() / ".arenamcp" / "cache"
GRAPH_PKL_PATH = CACHE_DIR / "synergy_graph.pkl"
GRAPH_JSON_PATH = CACHE_DIR / "synergy_graph.json"


class SynergyGraph:
    """NetworkX-based graph for analyzing card synergies and interactions.

    Nodes represent cards, edges represent synergy relationships.
    Edge weights indicate strength of synergy (0.0 to 1.0).
    """

    # Synergy keywords that indicate interactions
    SYNERGY_KEYWORDS = {
        "tribal": [
            "elf", "goblin", "merfolk", "zombie", "vampire", "dragon",
            "angel", "demon", "human", "wizard", "warrior", "eldrazi",
            "faerie", "rat", "spider", "knight", "soldier", "beast",
            "elemental", "dinosaur", "cat", "dog", "bird", "squirrel",
            "skeleton", "spirit", "rogue", "cleric", "shaman", "druid",
            "pirate", "scout", "mole", "badger", "sphinx",
        ],
        "mechanics": [
            "sacrifice", "draw", "discard", "counter", "destroy", "exile",
            "token", "ETB",
        ],
        "keywords": [
            "flying", "first strike", "deathtouch", "vigilance", "trample",
            "lifelink", "haste", "menace", "reach", "ward", "hexproof",
        ],
        "card_types": [
            "artifact", "creature", "enchantment", "instant", "sorcery",
            "planeswalker",
        ],
        "themes": [
            "graveyard", "lifegain", "+1/+1 counter", "ramp", "mill",
            "energy", "food", "treasure", "clue", "blood", "role",
            "modified", "delirium", "threshold",
        ],
    }

    def __init__(self) -> None:
        """Initialize the synergy graph."""
        self.graph = nx.Graph()
        self.card_index: dict[str, dict[str, Any]] = {}
        # Inverted index: "category:value" -> set of card names
        self.feature_index: dict[str, set[str]] = {}

    def add_card(self, card: dict[str, Any]) -> None:
        """Add a card to the synergy graph.

        Args:
            card: Card dictionary with name, oracle_text, type_line, etc.
        """
        card_name = card.get("name", "")
        if not card_name:
            return

        features = self._extract_card_features(card)

        self.graph.add_node(card_name, **features)
        self.card_index[card_name] = {"features": features, "card": card}
        self._update_index(card_name, features)

    def _update_index(self, card_name: str, features: dict[str, Any]) -> None:
        """Update the inverted index with a card's features."""
        for category in ("tribes", "mechanics", "themes", "keywords"):
            for value in features.get(category, []):
                key = f"{category[:-1]}:{value}"  # e.g. "tribe:elf"
                if key not in self.feature_index:
                    self.feature_index[key] = set()
                self.feature_index[key].add(card_name)

    def _extract_card_features(self, card: dict[str, Any]) -> dict[str, Any]:
        """Extract synergy-relevant features from a card."""
        oracle_text = card.get("oracle_text", "").lower()
        type_line = card.get("type_line", "").lower()
        keywords = [kw.lower() for kw in card.get("keywords", [])]

        features: dict[str, Any] = {
            "type_line": type_line,
            "oracle_text": oracle_text,
            "keywords": keywords,
            "colors": card.get("colors", []),
            "cmc": card.get("cmc", 0),
            "power": card.get("power"),
            "toughness": card.get("toughness"),
        }

        # Extract tribes
        tribes = []
        for tribe in self.SYNERGY_KEYWORDS["tribal"]:
            if tribe in type_line or tribe in oracle_text:
                tribes.append(tribe)
        features["tribes"] = tribes

        # Extract mechanics
        mechanics = []
        for mechanic in self.SYNERGY_KEYWORDS["mechanics"]:
            if mechanic.lower() in oracle_text:
                mechanics.append(mechanic)
        features["mechanics"] = mechanics

        # Extract themes
        themes = []
        for theme in self.SYNERGY_KEYWORDS["themes"]:
            if theme in oracle_text:
                themes.append(theme)
        features["themes"] = themes

        # Card types
        card_types = []
        for ctype in self.SYNERGY_KEYWORDS["card_types"]:
            if ctype in type_line:
                card_types.append(ctype)
        features["card_types"] = card_types

        return features

    def build_synergies(self) -> None:
        """Build synergy edges between all cards in the graph."""
        total = len(self.graph.nodes)
        logger.info(f"Building synergies for {total} cards...")

        cards = list(self.graph.nodes(data=True))
        edge_count = 0

        for i, (card_name, card_data) in enumerate(cards):
            if (i + 1) % 2000 == 0:
                logger.info(f"  Processed {i+1}/{total} cards... ({edge_count} edges)")

            candidates = self._get_candidates(card_name, card_data)

            for candidate_name in candidates:
                if candidate_name <= card_name:
                    continue

                candidate_data = self.graph.nodes[candidate_name]
                synergy_score = self._calculate_synergy(
                    card_name, card_data, candidate_name, candidate_data
                )

                if synergy_score > 0.45:
                    self.graph.add_edge(
                        card_name,
                        candidate_name,
                        weight=synergy_score,
                        synergy_types=self._get_synergy_types(card_data, candidate_data),
                    )
                    edge_count += 1

        logger.info(f"Created {edge_count} synergy relationships")

    def _get_candidates(self, card_name: str, card_data: dict[str, Any]) -> set[str]:
        """Get candidate cards that share at least one feature."""
        candidates: set[str] = set()

        for category in ("tribes", "mechanics", "themes", "keywords"):
            key_prefix = category[:-1]  # e.g. "tribe"
            for value in card_data.get(category, []):
                key = f"{key_prefix}:{value}"
                candidates.update(self.feature_index.get(key, set()))

        candidates.discard(card_name)
        return candidates

    def _calculate_synergy(
        self,
        card1_name: str,
        card1_data: dict[str, Any],
        card2_name: str,
        card2_data: dict[str, Any],
    ) -> float:
        """Calculate synergy score between two cards (0.0 to 1.0)."""
        score = 0.0

        # Tribal synergy (strong)
        tribes1 = set(card1_data.get("tribes", []))
        tribes2 = set(card2_data.get("tribes", []))
        if tribes1 & tribes2:
            score += 0.4

        # Mechanic synergy (medium)
        mechanics1 = set(card1_data.get("mechanics", []))
        mechanics2 = set(card2_data.get("mechanics", []))
        mechanic_overlap = len(mechanics1 & mechanics2)
        if mechanic_overlap > 0:
            score += 0.2 * min(mechanic_overlap, 2)

        # Theme synergy (medium)
        themes1 = set(card1_data.get("themes", []))
        themes2 = set(card2_data.get("themes", []))
        if themes1 & themes2:
            score += 0.2

        # Keyword synergy (weak)
        keywords1 = set(card1_data.get("keywords", []))
        keywords2 = set(card2_data.get("keywords", []))
        if keywords1 & keywords2:
            score += 0.1

        # Color identity synergy (weak bonus)
        colors1 = set(card1_data.get("colors", []))
        colors2 = set(card2_data.get("colors", []))
        if colors1 and colors2 and (colors1 & colors2):
            score += 0.05

        # Mana curve synergy (complementary costs)
        cmc1 = card1_data.get("cmc", 0)
        cmc2 = card2_data.get("cmc", 0)
        if abs(cmc1 - cmc2) >= 2 and cmc1 < 7 and cmc2 < 7:
            score += 0.05

        return min(score, 1.0)

    def _get_synergy_types(
        self, card1_data: dict[str, Any], card2_data: dict[str, Any]
    ) -> list[str]:
        """Get list of synergy types between two cards."""
        types = []
        if set(card1_data.get("tribes", [])) & set(card2_data.get("tribes", [])):
            types.append("tribal")
        if set(card1_data.get("mechanics", [])) & set(card2_data.get("mechanics", [])):
            types.append("mechanic")
        if set(card1_data.get("themes", [])) & set(card2_data.get("themes", [])):
            types.append("theme")
        if set(card1_data.get("keywords", [])) & set(card2_data.get("keywords", [])):
            types.append("keyword")
        return types

    def find_synergies_for_card(
        self, card_name: str, top_n: int = 10
    ) -> list[tuple[str, float, list[str]]]:
        """Find cards with highest synergy to the given card.

        Returns:
            List of (card_name, synergy_score, synergy_types) tuples.
        """
        if card_name not in self.graph:
            return []

        neighbors = []
        for neighbor in self.graph.neighbors(card_name):
            edge_data = self.graph[card_name][neighbor]
            weight = edge_data.get("weight", 0)
            synergy_types = edge_data.get("synergy_types", [])
            neighbors.append((neighbor, weight, synergy_types))

        neighbors.sort(key=lambda x: x[1], reverse=True)
        return neighbors[:top_n]

    def find_combo_pieces(self, card_name: str, threshold: float = 0.5) -> list[str]:
        """Find potential combo pieces for a card above a synergy threshold."""
        synergies = self.find_synergies_for_card(card_name, top_n=50)
        return [card for card, score, _ in synergies if score >= threshold]

    def get_cluster_recommendations(
        self, seed_cards: list[str], top_n: int = 10
    ) -> list[tuple[str, float]]:
        """Get card recommendations based on a cluster of seed cards.

        Aggregates synergy scores from all seed cards and returns
        the top candidates not already in the seed set.

        Args:
            seed_cards: List of card names to base recommendations on.
            top_n: Number of recommendations to return.

        Returns:
            List of (card_name, aggregate_score) tuples.
        """
        candidate_scores: dict[str, float] = {}
        seed_set = set(seed_cards)

        for seed_card in seed_cards:
            if seed_card not in self.graph:
                continue

            synergies = self.find_synergies_for_card(seed_card, top_n=50)
            for card, score, _ in synergies:
                if card in seed_set:
                    continue
                if card in candidate_scores:
                    candidate_scores[card] += score
                else:
                    candidate_scores[card] = score

        # Normalize by number of seed cards
        if seed_cards:
            for card in candidate_scores:
                candidate_scores[card] /= len(seed_cards)

        recommendations = sorted(
            candidate_scores.items(), key=lambda x: x[1], reverse=True
        )
        return recommendations[:top_n]

    def build_from_scryfall(self, scryfall: "ScryfallCache") -> None:
        """Build the synergy graph from Scryfall bulk data.

        Iterates the Scryfall cache's arena_index to populate nodes,
        then builds synergy edges.

        Args:
            scryfall: Initialized ScryfallCache with bulk data loaded.
        """
        logger.info("Building synergy graph from Scryfall bulk data...")
        start = time.time()

        count = 0
        for arena_id, card_data in scryfall._arena_index.items():
            name = card_data.get("name", "")
            if not name:
                continue
            # Skip tokens and other non-card objects
            if card_data.get("layout") in ("token", "double_faced_token", "art_series"):
                continue

            self.add_card({
                "name": name,
                "oracle_text": card_data.get("oracle_text", ""),
                "type_line": card_data.get("type_line", ""),
                "keywords": card_data.get("keywords", []),
                "colors": card_data.get("colors", []),
                "cmc": card_data.get("cmc", 0),
                "power": card_data.get("power"),
                "toughness": card_data.get("toughness"),
            })
            count += 1

        logger.info(f"Added {count} cards in {time.time() - start:.1f}s, building edges...")
        self.build_synergies()
        self.save()
        logger.info(f"Synergy graph complete: {self.stats()}")

    def save(self) -> None:
        """Save the synergy graph to disk (PKL + JSON)."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Save PKL (fast binary)
        try:
            logger.info(f"Saving graph to {GRAPH_PKL_PATH}...")
            with open(GRAPH_PKL_PATH, "wb") as f:
                pickle.dump(self.graph, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("Saved pickle cache.")
        except Exception as e:
            logger.error(f"Error saving pickle cache: {e}")

        # Save JSON (human-readable)
        try:
            logger.info(f"Saving graph to {GRAPH_JSON_PATH}...")
            with open(GRAPH_JSON_PATH, "w", encoding="utf-8") as f:
                f.write('{"nodes": [')
                nodes = list(self.graph.nodes(data=True))
                for i, (node, data) in enumerate(nodes):
                    if i > 0:
                        f.write(",")
                    f.write(json.dumps([node, data]))
                f.write('], "edges": [')
                edges = list(self.graph.edges(data=True))
                for i, (u, v, data) in enumerate(edges):
                    if i > 0:
                        f.write(",")
                    f.write(json.dumps([u, v, data]))
                f.write("]}")
            logger.info(f"Saved {len(nodes)} nodes and {len(edges)} edges to JSON")
        except Exception as e:
            logger.error(f"Error saving graph JSON: {e}")

    def load(self) -> bool:
        """Load synergy graph from disk. Tries PKL first, then JSON.

        Returns:
            True if loaded successfully, False otherwise.
        """
        # Try PKL first (fast)
        if GRAPH_PKL_PATH.exists():
            try:
                logger.info(f"Loading binary cache from {GRAPH_PKL_PATH}...")
                start = time.time()
                with open(GRAPH_PKL_PATH, "rb") as f:
                    self.graph = pickle.load(f)

                # Rebuild indexes
                self.card_index = {}
                for node, data in self.graph.nodes(data=True):
                    self.card_index[node] = {"features": data}
                    self._update_index(node, data)

                logger.info(f"Loaded in {time.time() - start:.2f}s")
                return True
            except Exception as e:
                logger.warning(f"Error loading pickle cache: {e}. Falling back to JSON.")

        # Fallback to JSON
        if not GRAPH_JSON_PATH.exists():
            return False

        try:
            logger.info(f"Loading JSON from {GRAPH_JSON_PATH}...")
            with open(GRAPH_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.graph = nx.Graph()
            for node_name, node_data in data.get("nodes", []):
                self.graph.add_node(node_name, **node_data)
            for u, v, edge_data in data.get("edges", []):
                self.graph.add_edge(u, v, **edge_data)

            # Rebuild indexes
            self.card_index = {
                node: {"features": ndata}
                for node, ndata in self.graph.nodes(data=True)
            }
            for node, ndata in self.graph.nodes(data=True):
                self._update_index(node, ndata)

            logger.info(f"Loaded JSON. Saving PKL cache for next time...")
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with open(GRAPH_PKL_PATH, "wb") as f:
                    pickle.dump(self.graph, f, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as e:
                logger.warning(f"Failed to save PKL cache: {e}")

            stats = self.stats()
            logger.info(f"Graph loaded: {stats['num_cards']} cards, {stats['num_synergies']} edges")
            return True

        except Exception as e:
            logger.error(f"Error loading synergy graph: {e}")
            return False

    def stats(self) -> dict[str, Any]:
        """Get statistics about the synergy graph."""
        num_nodes = len(self.graph.nodes)
        num_edges = len(self.graph.edges)
        return {
            "num_cards": num_nodes,
            "num_synergies": num_edges,
            "avg_synergies_per_card": (
                2 * num_edges / num_nodes if num_nodes > 0 else 0
            ),
            "density": nx.density(self.graph),
        }


# Lazy singleton
_synergy_graph: Optional[SynergyGraph] = None


def get_synergy_graph() -> Optional[SynergyGraph]:
    """Get or create the singleton SynergyGraph instance.

    Loads from disk cache if available. Returns None if no graph exists
    (call build_from_scryfall to create one).
    """
    global _synergy_graph
    if _synergy_graph is None:
        logger.info("Initializing SynergyGraph singleton...")
        instance = SynergyGraph()
        if instance.load():
            _synergy_graph = instance
            stats = _synergy_graph.stats()
            logger.info(
                f"SynergyGraph ready ({stats['num_cards']} cards, "
                f"{stats['num_synergies']} synergies)"
            )
        else:
            logger.info("SynergyGraph not found on disk. Build with build_from_scryfall().")
            return None
    return _synergy_graph
