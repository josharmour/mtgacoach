"""Vision-based UI Detection with Cached Layout Analysis.

Hybrid VLM approach inspired by VLA-Cache (arxiv.org/abs/2502.02175):
  - Cache layout scans per game phase (static elements don't move)
  - Use fast local VLM (Ollama) for element detection (~500ms)
  - Fall back to cloud VLM for complex/ambiguous cases (~2-4s)

Replaces static coordinate heuristics with actual visual understanding
while keeping latency manageable through aggressive caching.

Usage:
    mapper = VisionMapper(ollama_model="qwen2.5-vl:3b")
    mapper.set_cloud_backend(proxy_backend)  # optional cloud fallback

    # Full scan at phase change — caches all element positions
    mapper.scan_layout(screenshot_bytes, game_state)

    # Fast cached lookup for individual elements
    coord = mapper.get_element_coord("Swamp", "hand")
    coord = mapper.get_element_coord("Done", "button")
    coord = mapper.get_element_coord("Grizzly Bears", "battlefield_yours")
"""

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from arenamcp.screen_mapper import ScreenCoord, FixedCoordinates, ScreenMapper

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layout Cache
# ---------------------------------------------------------------------------

@dataclass
class CachedElement:
    """A UI element with its cached screen position."""
    name: str
    zone: str  # "hand", "battlefield_yours", "battlefield_opp", "button", "option", "draft"
    coord: ScreenCoord
    confidence: float = 1.0  # 0.0-1.0, from VLM response
    timestamp: float = 0.0
    instance_id: Optional[int] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


@dataclass
class LayoutSnapshot:
    """Cached layout of all detected UI elements from a VLM scan."""
    elements: dict[str, CachedElement] = field(default_factory=dict)
    phase: str = ""
    turn: int = 0
    scan_time: float = 0.0
    screenshot_hash: str = ""

    def get(self, name: str, zone: Optional[str] = None) -> Optional[CachedElement]:
        """Look up a cached element by name, optionally filtered by zone."""
        name_lower = name.lower().strip()

        # Exact key match first
        key = f"{zone}:{name_lower}" if zone else name_lower
        if key in self.elements:
            return self.elements[key]

        # Search by name across zones
        for k, elem in self.elements.items():
            if elem.name.lower() == name_lower:
                if zone is None or elem.zone == zone:
                    return elem

        # Partial match
        for k, elem in self.elements.items():
            if name_lower in elem.name.lower() or elem.name.lower() in name_lower:
                if zone is None or elem.zone == zone:
                    return elem

        return None

    def is_stale(self, max_age: float = 30.0) -> bool:
        """Check if the cache is too old."""
        return (time.time() - self.scan_time) > max_age

    @property
    def age_seconds(self) -> float:
        return time.time() - self.scan_time


# ---------------------------------------------------------------------------
# VLM Prompts
# ---------------------------------------------------------------------------

LAYOUT_SCAN_PROMPT = """You are an MTG Arena screen analyzer. Analyze this screenshot and locate EVERY interactive element visible.

For each element, provide its EXACT center position as normalized coordinates (0.0 to 1.0, where 0,0 is top-left and 1,1 is bottom-right).

Categorize each element into one of these zones:
- "button": UI buttons (Pass, Done, Resolve, Keep, Mulligan, etc.)
- "hand": Cards in the player's hand (bottom of screen)
- "battlefield_yours": Player's permanents on the battlefield
- "battlefield_opp": Opponent's permanents on the battlefield
- "option": Modal choices, scry options, or selection prompts
- "draft": Cards in a draft pack

Output ONLY a JSON object with this structure:
{
  "elements": [
    {"name": "Card Name or Button Label", "zone": "hand", "x": 0.35, "y": 0.92, "confidence": 0.95},
    {"name": "Done", "zone": "button", "x": 0.92, "y": 0.88, "confidence": 0.99}
  ],
  "phase_hint": "main_phase|combat|mulligan|draft|unknown"
}

Be precise with coordinates. Include ALL visible cards and buttons."""

ELEMENT_FIND_PROMPT = """You are an MTG Arena UI locator.
Find the EXACT center of "{element_name}" in the {zone_hint} area of this screenshot.
Output ONLY a JSON object: {{"x": 0.45, "y": 0.58, "confidence": 0.9}}
If the element is NOT visible, output: {{"x": null, "y": null, "confidence": 0.0}}"""

DECISION_DETECT_PROMPT = """You are an MTG Arena UI analyzer. Look at this screenshot and determine if the game is waiting for the player to make a decision.

Signs of a pending decision:
- A popup/dialog asking the player to choose (e.g., "Choose a creature", "Select a card to discard")
- Highlighted/glowing cards that can be selected
- A prompt bar with instruction text (usually in the center or bottom of the screen)
- Modal selection UI with multiple options
- Cards fanned out for selection (e.g., scry, surveil, search library)
- A "Submit" or "Done" button visible alongside selectable options

Signs there is NO pending decision:
- Normal gameplay view with no prompts
- The game timer is running but no selection UI is visible
- Only the pass/resolve button is shown (normal priority)

Output ONLY a JSON object:
{
  "waiting_for_input": true,
  "decision_type": "choose_creature|discard|select_target|modal_choice|scry|surveil|search_library|exile_choice|sacrifice|unknown",
  "prompt_text": "the visible prompt text if readable, or empty string",
  "num_options": 3,
  "confidence": 0.85
}

If NO decision is pending:
{"waiting_for_input": false, "decision_type": null, "prompt_text": "", "num_options": 0, "confidence": 0.9}"""

CARD_IDENTIFY_PROMPT = """You are an MTG Arena card identifier. Look at this screenshot of an MTG Arena game.

I need you to identify the card(s) that are visible but whose names I cannot determine from the game log.
The card(s) I need identified are in the following zone: {zone}.
{hint}

Read the card name text visible on the card art, title bar, or tooltip. If multiple unknown cards exist in
that zone, list all of them.

Output ONLY a JSON object:
{{
  "cards": [
    {{"name": "Exact Card Name", "confidence": 0.9}},
    {{"name": "Another Card", "confidence": 0.7}}
  ]
}}

If you cannot read any card names clearly:
{{"cards": []}}"""


# ---------------------------------------------------------------------------
# Ollama VLM Client
# ---------------------------------------------------------------------------

class OllamaVLM:
    """Fast local VLM via Ollama for screen analysis."""

    def __init__(
        self,
        model: str = "qwen2.5-vl:3b",
        endpoint: str = "http://localhost:11434",
        timeout: float = 20.0,
    ):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        """Check if Ollama is running and the model is available."""
        if self._available is not None:
            return self._available
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{self.endpoint}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                models = [m.get("name", "") for m in data.get("models", [])]
                # Check if our model (or a prefix of it) is available
                prefix = self.model.split(":")[0]
                matched = None
                for m in models:
                    if self.model in m or m.startswith(prefix):
                        matched = m
                        break
                # Fuzzy: strip hyphens to handle naming variants
                # e.g. "qwen2.5-vl:3b" matches "qwen2.5vl:3b"
                if matched is None:
                    norm = self.model.replace("-", "")
                    norm_prefix = prefix.replace("-", "")
                    for m in models:
                        mn = m.replace("-", "")
                        if norm in mn or mn.startswith(norm_prefix):
                            matched = m
                            break
                if matched:
                    if matched != self.model:
                        logger.info(
                            f"Ollama model '{self.model}' resolved to '{matched}'"
                        )
                        self.model = matched
                    self._available = True
                else:
                    self._available = False
                    logger.warning(
                        f"Ollama model '{self.model}' not found. "
                        f"Available: {models}. Run: ollama pull {self.model}"
                    )
                return self._available
        except Exception as e:
            logger.info(f"Ollama not available: {e}")
            self._available = False
            return False

    def analyze(self, prompt: str, image_bytes: bytes) -> Optional[dict]:
        """Send image + prompt to Ollama and parse JSON response."""
        if not self.available:
            return None

        try:
            import urllib.request

            b64_image = base64.b64encode(image_bytes).decode("utf-8")

            payload = json.dumps({
                "model": self.model,
                "prompt": prompt,
                "images": [b64_image],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 2048,
                },
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self.endpoint}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read())

            elapsed_ms = (time.perf_counter() - start) * 1000
            response_text = result.get("response", "")
            logger.info(f"[Ollama] {self.model}: {elapsed_ms:.0f}ms, {len(response_text)} chars")

            # Extract JSON from response (may be wrapped in markdown fences)
            return self._parse_json(response_text)

        except Exception as e:
            logger.error(f"Ollama analyze failed: {e}")
            return None

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """Extract JSON from VLM response text."""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code fence
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try finding any JSON object
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                # Clean trailing commas
                cleaned = re.sub(r',\s*([}\]])', r'\1', match.group(0))
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

        return None


# ---------------------------------------------------------------------------
# VisionMapper — The Main Class
# ---------------------------------------------------------------------------

class VisionMapper:
    """Vision-based coordinate mapper with caching.

    Three-tier resolution:
      1. Layout cache (< 1ms) — from recent VLM scan of entire screen
      2. Local VLM    (~500ms) — Ollama for targeted element search
      3. Cloud VLM    (~2-4s)  — ProxyBackend for complex/ambiguous cases

    Cache invalidation triggers:
      - Phase change (main → combat → end)
      - Turn change
      - Significant state change (hand size, battlefield count)
      - Manual invalidation
      - Age-based expiry (default 30s)
    """

    def __init__(
        self,
        ollama_model: str = "qwen2.5vl:3b",
        ollama_endpoint: str = "http://localhost:11434",
        cache_max_age: float = 30.0,
        enable_local_vlm: bool = True,
        enable_cloud_vlm: bool = True,
    ):
        self._local_vlm = OllamaVLM(ollama_model, ollama_endpoint) if enable_local_vlm else None
        self._cloud_backend: Any = None  # Set via set_cloud_backend()
        self._enable_cloud = enable_cloud_vlm
        self._cache_max_age = cache_max_age

        # Layout cache
        self._cache = LayoutSnapshot()
        self._last_phase = ""
        self._last_turn = 0
        self._last_hand_size = 0
        self._last_bf_count = 0

        # Fallback: keep the static mapper for buttons that never move
        self._static_mapper = ScreenMapper()

        # Stats
        self._stats = {
            "cache_hits": 0,
            "local_vlm_calls": 0,
            "cloud_vlm_calls": 0,
            "static_fallbacks": 0,
            "total_scans": 0,
        }

        logger.info(
            f"VisionMapper initialized: local_vlm={'enabled' if self._local_vlm else 'disabled'}, "
            f"cloud_vlm={'enabled' if enable_cloud_vlm else 'disabled'}, "
            f"cache_max_age={cache_max_age}s"
        )

    def set_cloud_backend(self, backend: Any) -> None:
        """Set the cloud VLM backend (e.g., ProxyBackend with vision support)."""
        self._cloud_backend = backend

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def cache_age(self) -> float:
        return self._cache.age_seconds

    @property
    def cache_size(self) -> int:
        return len(self._cache.elements)

    # ------------------------------------------------------------------
    # Cache Invalidation
    # ------------------------------------------------------------------

    def needs_rescan(self, game_state: dict[str, Any]) -> bool:
        """Determine if the layout cache should be refreshed.

        Triggers rescan on:
          - Phase/step change
          - Turn number change
          - Hand size change (drew/played a card)
          - Battlefield count change
          - Cache expired by age
        """
        if self._cache.is_stale(self._cache_max_age):
            logger.debug("Cache stale by age")
            return True

        phase = game_state.get("phase", "") + "/" + game_state.get("step", "")
        turn = game_state.get("turn_number", 0)
        hand_size = len(game_state.get("hand", []))
        bf = game_state.get("battlefield", [])
        bf_count = len(bf) if isinstance(bf, list) else 0

        changed = (
            phase != self._last_phase
            or turn != self._last_turn
            or hand_size != self._last_hand_size
            or bf_count != self._last_bf_count
        )

        if changed:
            logger.debug(
                f"State change detected: phase={self._last_phase}->{phase}, "
                f"turn={self._last_turn}->{turn}, "
                f"hand={self._last_hand_size}->{hand_size}, "
                f"bf={self._last_bf_count}->{bf_count}"
            )

        return changed

    def invalidate_cache(self) -> None:
        """Force cache invalidation."""
        self._cache = LayoutSnapshot()
        logger.info("Layout cache invalidated")

    # ------------------------------------------------------------------
    # Full Layout Scan (Tier 2 or 3)
    # ------------------------------------------------------------------

    def scan_layout(
        self,
        screenshot_bytes: bytes,
        game_state: dict[str, Any],
        force: bool = False,
    ) -> LayoutSnapshot:
        """Perform a full layout scan of the MTGA screen.

        Captures positions of ALL visible elements and caches them.
        Called once per phase change, not per action.

        Args:
            screenshot_bytes: PNG screenshot of MTGA window.
            game_state: Current game state for cache key context.
            force: Force rescan even if cache is fresh.

        Returns:
            The new LayoutSnapshot.
        """
        if not force and not self.needs_rescan(game_state):
            logger.debug(f"Cache still valid ({self._cache.age_seconds:.1f}s old, {self.cache_size} elements)")
            return self._cache

        self._stats["total_scans"] += 1
        start = time.perf_counter()
        result = None

        # Try local VLM first (fast)
        if self._local_vlm and self._local_vlm.available:
            self._stats["local_vlm_calls"] += 1
            result = self._local_vlm.analyze(LAYOUT_SCAN_PROMPT, screenshot_bytes)

        # Fall back to cloud VLM
        if result is None and self._enable_cloud and self._cloud_backend:
            self._stats["cloud_vlm_calls"] += 1
            result = self._cloud_scan(screenshot_bytes)

        elapsed_ms = (time.perf_counter() - start) * 1000

        if result and "elements" in result:
            self._cache = self._build_snapshot(result, game_state)
            logger.info(
                f"Layout scan: {len(self._cache.elements)} elements in {elapsed_ms:.0f}ms "
                f"(phase: {self._cache.phase})"
            )
        else:
            logger.warning(f"Layout scan returned no elements ({elapsed_ms:.0f}ms)")
            # Don't wipe cache on failed scan — stale data is better than none
            # But do update the state tracking so we don't scan every call
            pass

        # Update state tracking regardless
        self._last_phase = game_state.get("phase", "") + "/" + game_state.get("step", "")
        self._last_turn = game_state.get("turn_number", 0)
        self._last_hand_size = len(game_state.get("hand", []))
        bf = game_state.get("battlefield", [])
        self._last_bf_count = len(bf) if isinstance(bf, list) else 0

        return self._cache

    def _cloud_scan(self, screenshot_bytes: bytes) -> Optional[dict]:
        """Use cloud VLM backend for layout scan."""
        try:
            if not hasattr(self._cloud_backend, "complete_with_image"):
                return None

            response = self._cloud_backend.complete_with_image(
                LAYOUT_SCAN_PROMPT,
                "Analyze this MTG Arena screenshot. Find all interactive elements.",
                screenshot_bytes,
            )
            return OllamaVLM._parse_json(response)
        except Exception as e:
            logger.error(f"Cloud layout scan failed: {e}")
            return None

    def _build_snapshot(
        self, vlm_result: dict, game_state: dict[str, Any]
    ) -> LayoutSnapshot:
        """Convert VLM response into a LayoutSnapshot."""
        snapshot = LayoutSnapshot(
            phase=vlm_result.get("phase_hint", "unknown"),
            turn=game_state.get("turn_number", 0),
            scan_time=time.time(),
        )

        for elem in vlm_result.get("elements", []):
            name = elem.get("name", "").strip()
            zone = elem.get("zone", "unknown")
            x = elem.get("x")
            y = elem.get("y")
            confidence = elem.get("confidence", 0.5)

            if not name or x is None or y is None:
                continue

            # Validate coordinate ranges
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                logger.warning(f"VLM returned out-of-range coord for '{name}': ({x}, {y})")
                continue

            key = f"{zone}:{name.lower()}"
            snapshot.elements[key] = CachedElement(
                name=name,
                zone=zone,
                coord=ScreenCoord(x, y, f"Vision: {name}"),
                confidence=confidence,
                timestamp=time.time(),
            )

        return snapshot

    # ------------------------------------------------------------------
    # Element Lookup (Tier 1 → 2 → 3)
    # ------------------------------------------------------------------

    def get_element_coord(
        self,
        name: str,
        zone: Optional[str] = None,
        screenshot_bytes: Optional[bytes] = None,
        game_state: Optional[dict] = None,
    ) -> Optional[ScreenCoord]:
        """Get the screen coordinate of a named element.

        Resolution order:
          1. Layout cache (instant)
          2. Targeted local VLM query (fast, if screenshot provided)
          3. Cloud VLM query (slow, if screenshot provided)
          4. Static coordinate fallback (buttons only)

        Args:
            name: Element name (card name or button label).
            zone: Optional zone filter ("hand", "battlefield_yours", "button", etc.)
            screenshot_bytes: Current screenshot for VLM queries (optional).
            game_state: Current game state for scan triggers (optional).

        Returns:
            ScreenCoord or None.
        """
        # Tier 1: Cache lookup
        cached = self._cache.get(name, zone)
        if cached and cached.confidence >= 0.5:
            self._stats["cache_hits"] += 1
            logger.debug(f"Cache hit: '{name}' -> ({cached.coord.x:.3f}, {cached.coord.y:.3f}) [{cached.age_seconds:.1f}s old]")
            return cached.coord

        # Tier 2: Targeted local VLM query
        if screenshot_bytes and self._local_vlm and self._local_vlm.available:
            coord = self._targeted_vlm_query(name, zone, screenshot_bytes, local=True)
            if coord:
                return coord

        # Tier 3: Cloud VLM query
        if screenshot_bytes and self._enable_cloud and self._cloud_backend:
            coord = self._targeted_vlm_query(name, zone, screenshot_bytes, local=False)
            if coord:
                return coord

        # Tier 4: Static fallback (buttons only)
        static_coord = FixedCoordinates.get(name.lower().replace(" ", "_"))
        if static_coord:
            self._stats["static_fallbacks"] += 1
            logger.debug(f"Static fallback: '{name}' -> ({static_coord.x:.3f}, {static_coord.y:.3f})")
            return static_coord

        logger.warning(f"Could not locate '{name}' (zone={zone}) via any method")
        return None

    def _targeted_vlm_query(
        self,
        name: str,
        zone: Optional[str],
        screenshot_bytes: bytes,
        local: bool = True,
    ) -> Optional[ScreenCoord]:
        """Query VLM for a single element's position."""
        zone_hint = zone or "any"
        prompt = ELEMENT_FIND_PROMPT.format(element_name=name, zone_hint=zone_hint)

        result = None
        if local and self._local_vlm:
            self._stats["local_vlm_calls"] += 1
            result = self._local_vlm.analyze(prompt, screenshot_bytes)
        elif not local and self._cloud_backend:
            self._stats["cloud_vlm_calls"] += 1
            try:
                if hasattr(self._cloud_backend, "complete_with_image"):
                    response = self._cloud_backend.complete_with_image(
                        "You are an MTG Arena UI locator. Output only JSON.",
                        prompt,
                        screenshot_bytes,
                    )
                    result = OllamaVLM._parse_json(response)
            except Exception as e:
                logger.error(f"Cloud targeted query failed: {e}")

        if result:
            x = result.get("x")
            y = result.get("y")
            confidence = result.get("confidence", 0.5)

            if x is not None and y is not None and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                coord = ScreenCoord(x, y, f"Vision: {name}")

                # Cache the result for future lookups
                key = f"{zone or 'unknown'}:{name.lower()}"
                self._cache.elements[key] = CachedElement(
                    name=name,
                    zone=zone or "unknown",
                    coord=coord,
                    confidence=confidence,
                    timestamp=time.time(),
                )

                tier = "local VLM" if local else "cloud VLM"
                logger.info(f"Targeted {tier}: '{name}' -> ({x:.3f}, {y:.3f}) [conf={confidence:.2f}]")
                return coord

        return None

    # ------------------------------------------------------------------
    # Convenience Methods (matching ScreenMapper API)
    # ------------------------------------------------------------------

    def get_button_coord(self, name: str) -> Optional[ScreenCoord]:
        """Get button coordinate — cache first, static fallback."""
        return self.get_element_coord(name, zone="button")

    def get_card_in_hand_coord(
        self,
        card_name: str,
        hand_cards: list[dict[str, Any]],
        game_state: dict[str, Any],
        screenshot_bytes: Optional[bytes] = None,
    ) -> Optional[ScreenCoord]:
        """Get hand card coordinate — cache first, VLM if available, static fallback."""
        # Try vision path
        coord = self.get_element_coord(card_name, zone="hand", screenshot_bytes=screenshot_bytes)
        if coord:
            return coord

        # Fall back to static heuristic
        self._stats["static_fallbacks"] += 1
        return self._static_mapper.get_card_in_hand_coord(card_name, hand_cards, game_state)

    def get_permanent_coord(
        self,
        card_name: str,
        instance_id: Optional[int],
        battlefield: list[dict[str, Any]],
        owner_seat: int,
        local_seat: int,
        screenshot_bytes: Optional[bytes] = None,
    ) -> Optional[ScreenCoord]:
        """Get battlefield permanent coordinate — cache first, VLM, static fallback."""
        is_yours = owner_seat == local_seat
        zone = "battlefield_yours" if is_yours else "battlefield_opp"

        coord = self.get_element_coord(card_name, zone=zone, screenshot_bytes=screenshot_bytes)
        if coord:
            return coord

        # Fall back to static heuristic
        self._stats["static_fallbacks"] += 1
        return self._static_mapper.get_permanent_coord(
            card_name, instance_id, battlefield, owner_seat, local_seat
        )

    def get_option_coord(
        self,
        option_index: int,
        total_options: int,
        context: str = "",
        screenshot_bytes: Optional[bytes] = None,
    ) -> Optional[ScreenCoord]:
        """Get modal option coordinate."""
        # Try cache for specific option labels
        option_name = f"Option {option_index + 1}"
        coord = self.get_element_coord(option_name, zone="option", screenshot_bytes=screenshot_bytes)
        if coord:
            return coord

        # Static fallback
        self._stats["static_fallbacks"] += 1
        return self._static_mapper.get_option_coord(option_index, total_options, context)

    def get_draft_card_coord(
        self,
        card_name: str,
        card_index: int,
        pack_size: int,
        screenshot_bytes: Optional[bytes] = None,
    ) -> Optional[ScreenCoord]:
        """Get draft card coordinate."""
        coord = self.get_element_coord(card_name, zone="draft", screenshot_bytes=screenshot_bytes)
        if coord:
            return coord

        # Static fallback
        self._stats["static_fallbacks"] += 1
        return self._static_mapper.get_draft_card_coord(card_index, pack_size)

    # ------------------------------------------------------------------
    # Delegation for ScreenMapper-compatible interface
    # ------------------------------------------------------------------

    def get_mtga_window(self) -> Optional[tuple[int, int, int, int]]:
        return self._static_mapper.get_mtga_window()

    def refresh_window(self) -> Optional[tuple[int, int, int, int]]:
        return self._static_mapper.refresh_window()

    @property
    def window_rect(self) -> Optional[tuple[int, int, int, int]]:
        return self._static_mapper.window_rect

    def get_card_coord_via_vision(
        self, card_name: str, screenshot_bytes: bytes, backend: Any
    ) -> Optional[ScreenCoord]:
        """Legacy compatibility — routes through the tiered system."""
        return self.get_element_coord(
            card_name, screenshot_bytes=screenshot_bytes
        )

    # ------------------------------------------------------------------
    # Decision Detection Watchdog
    # ------------------------------------------------------------------

    def detect_pending_decision(
        self, screenshot_bytes: bytes
    ) -> Optional[dict[str, Any]]:
        """Use VLM to check if the game is waiting for player input.

        This catches decision prompts that the log parser missed —
        card-specific choices like "choose a creature to exile",
        "select a card to discard", modal choices on adventure cards, etc.

        Args:
            screenshot_bytes: PNG screenshot of MTGA window.

        Returns:
            Dict with decision info if a decision is detected:
                {
                    "waiting_for_input": True,
                    "decision_type": "choose_creature",
                    "prompt_text": "Choose a creature to exile",
                    "num_options": 3,
                    "confidence": 0.85,
                }
            None if no decision detected or VLM unavailable.
        """
        result = None

        # Try local VLM first (fast)
        if self._local_vlm and self._local_vlm.available:
            self._stats["local_vlm_calls"] += 1
            result = self._local_vlm.analyze(DECISION_DETECT_PROMPT, screenshot_bytes)

        # Fall back to cloud
        if result is None and self._enable_cloud and self._cloud_backend:
            self._stats["cloud_vlm_calls"] += 1
            try:
                if hasattr(self._cloud_backend, "complete_with_image"):
                    response = self._cloud_backend.complete_with_image(
                        "You are an MTG Arena UI analyzer. Output only JSON.",
                        DECISION_DETECT_PROMPT,
                        screenshot_bytes,
                    )
                    result = OllamaVLM._parse_json(response)
            except Exception as e:
                logger.error(f"Cloud decision detection failed: {e}")

        if not result:
            return None

        waiting = result.get("waiting_for_input", False)
        confidence = result.get("confidence", 0.0)

        if waiting and confidence >= 0.6:
            logger.info(
                f"Vision detected pending decision: "
                f"type={result.get('decision_type')}, "
                f"prompt='{result.get('prompt_text', '')}', "
                f"options={result.get('num_options', 0)}, "
                f"confidence={confidence:.2f}"
            )
            return result

        return None  # No decision detected

    def identify_unknown_cards(
        self, screenshot_bytes: bytes, zone: str, hint: str = ""
    ) -> list[dict[str, Any]]:
        """Use VLM to identify cards that the log parser couldn't resolve.

        Args:
            screenshot_bytes: PNG screenshot of MTGA window.
            zone: Where the unknown cards are ("hand", "battlefield", "stack", etc.)
            hint: Extra context like "grpId=176656, appears to be a creature"

        Returns:
            List of {"name": "Card Name", "confidence": 0.9} dicts,
            or empty list if VLM unavailable or can't identify.
        """
        prompt = CARD_IDENTIFY_PROMPT.format(
            zone=zone,
            hint=f"Hint: {hint}" if hint else "",
        )

        result = None

        # Try local VLM first
        if self._local_vlm and self._local_vlm.available:
            self._stats["local_vlm_calls"] += 1
            result = self._local_vlm.analyze(prompt, screenshot_bytes)

        # Fall back to cloud
        if result is None and self._enable_cloud and self._cloud_backend:
            self._stats["cloud_vlm_calls"] += 1
            try:
                if hasattr(self._cloud_backend, "complete_with_image"):
                    response = self._cloud_backend.complete_with_image(
                        "You are an MTG card identifier. Output only JSON.",
                        prompt,
                        screenshot_bytes,
                    )
                    result = OllamaVLM._parse_json(response)
            except Exception as e:
                logger.error(f"Cloud card identification failed: {e}")

        if not result:
            return []

        cards = result.get("cards", [])
        identified = [c for c in cards if c.get("name") and c.get("confidence", 0) >= 0.6]
        if identified:
            logger.info(
                f"Vision identified {len(identified)} card(s) in {zone}: "
                + ", ".join(f"{c['name']} ({c['confidence']:.0%})" for c in identified)
            )
        return identified
