"""Hybrid UI Detection for Autopilot Mode.

Maps game actions to screen coordinates using resolution-aware proportional
positioning for buttons, arc-based geometry for hand cards, and vision/LLM
fallback for dynamic elements.

Button positions are derived from MTGA UI layout analysis:
- Primary/Secondary prompt buttons use a RectTransform-anchored layout in
  the bottom-right quadrant.
- Mulligan Keep/Mulligan buttons live inside a centred browser overlay
  (``MulliganBrowser``).
- Aspect-ratio adjustments compensate for the pillar-boxing that MTGA
  applies on non-16:9 displays.

All coordinates are normalized to the MTGA window (0.0-1.0 range) and
converted to absolute pixel coordinates at click time.
"""

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScreenCoord:
    """A normalized screen coordinate within the MTGA window.

    x, y are in range 0.0-1.0, relative to the MTGA window.
    """
    x: float
    y: float
    description: str = ""

    def to_absolute(self, window_rect: tuple[int, int, int, int]) -> tuple[int, int]:
        """Convert normalized coords to absolute screen pixels.

        Args:
            window_rect: (left, top, width, height) of the MTGA window.

        Returns:
            (abs_x, abs_y) in screen pixels.
        """
        left, top, width, height = window_rect
        abs_x = int(left + self.x * width)
        abs_y = int(top + self.y * height)
        logger.info(f"Coord: norm({self.x:.3f}, {self.y:.3f}) -> abs({abs_x}, {abs_y}) [rect: {window_rect}]")
        return (abs_x, abs_y)


class ButtonCoordinates:
    """Resolution-aware button positions derived from MTGA RE analysis.

    Positions are computed proportionally rather than hardcoded, with
    aspect-ratio compensation for non-16:9 displays.

    Architecture:

    * **Prompt buttons** (Pass / Resolve / Done / Submit Attackers etc.)
      live in a ``_buttonsLayout`` container anchored to
      ``_promptButtonsAnchorPoint`` in the bottom-right quadrant.
      ``UIManager.SpawnButtons`` creates a *primary* button (main action)
      and a *secondary* button (cancel / Resolve-All).  Both are children
      of the same layout, so they share a Y position and are separated
      horizontally.

    * **Mulligan buttons** (Keep / Mulligan) are embedded in a centred
      ``MulliganBrowser`` overlay, not in the prompt-button layout.

    * **Browser buttons** (Scry Top/Bottom, etc.) appear in centred
      browser overlays at roughly mid-screen height.

    When the window is not 16:9, MTGA pillar-boxes (adds black bars on the
    sides for wider ratios) or letter-boxes (bars top/bottom for taller
    ratios).  We compute the playable viewport inset and map coordinates
    inside it.
    """

    # ---- reference aspect ratio (MTGA's native layout target) ----------
    _REF_ASPECT = 16.0 / 9.0  # 1.7778

    # ---- 16:9 baseline positions (normalised to the playable viewport) --
    # Primary prompt button: bottom-right area
    # From RE: _primaryButton is to the right of _secondaryButton in
    # _buttonsLayout, anchored via _promptButtonsAnchorPoint.
    _PRIMARY_X_16_9 = 0.78
    _PRIMARY_Y_16_9 = 0.85

    # Secondary prompt button: slightly left of primary
    _SECONDARY_X_16_9 = 0.68
    _SECONDARY_Y_16_9 = 0.85

    # Mulligan browser buttons (centred overlay)
    _KEEP_X_16_9 = 0.58
    _KEEP_Y_16_9 = 0.68
    _MULL_X_16_9 = 0.42
    _MULL_Y_16_9 = 0.68

    # Scry/Surveil browser buttons (centred overlay)
    _SCRY_TOP_X_16_9 = 0.56
    _SCRY_TOP_Y_16_9 = 0.55
    _SCRY_BOT_X_16_9 = 0.44
    _SCRY_BOT_Y_16_9 = 0.55

    # Zone centres
    _HAND_CENTER_X = 0.50
    _HAND_CENTER_Y = 0.92
    _BF_YOUR_Y = 0.65
    _BF_OPP_Y = 0.35

    @staticmethod
    def _viewport_inset(aspect: float) -> tuple[float, float, float, float]:
        """Return (left, top, width, height) of the 16:9 playable viewport.

        All values are normalised 0-1 fractions of the full window.

        MTGA enforces a 16:9 playable area.  On wider monitors it
        pillar-boxes (black bars left+right); on taller monitors it
        letter-boxes (bars top+bottom).  The UI elements are laid out
        inside this inner viewport.

        Returns the identity (0, 0, 1, 1) for exactly 16:9.
        """
        ref = ButtonCoordinates._REF_ASPECT
        if abs(aspect - ref) < 0.02:
            # Close enough to 16:9 — no adjustment needed
            return (0.0, 0.0, 1.0, 1.0)

        if aspect > ref:
            # Wider than 16:9 (e.g. 21:9) — pillar-box
            vp_width = ref / aspect           # fraction of window used
            bar = (1.0 - vp_width) / 2.0
            return (bar, 0.0, vp_width, 1.0)
        else:
            # Taller than 16:9 (e.g. 16:10, 4:3) — letter-box
            vp_height = aspect / ref          # fraction of window used
            bar = (1.0 - vp_height) / 2.0
            return (0.0, bar, 1.0, vp_height)

    @classmethod
    def _to_window_coord(
        cls,
        vp_x: float,
        vp_y: float,
        aspect: float,
    ) -> tuple[float, float]:
        """Convert a viewport-relative coord to a window-relative coord.

        ``vp_x``, ``vp_y`` are in [0, 1] relative to the 16:9 playable
        viewport.  Returns (win_x, win_y) in [0, 1] relative to the full
        window, accounting for pillar-/letter-boxing.
        """
        left, top, w, h = cls._viewport_inset(aspect)
        return (left + vp_x * w, top + vp_y * h)

    @classmethod
    def get(
        cls,
        name: str,
        aspect: Optional[float] = None,
    ) -> Optional[ScreenCoord]:
        """Look up a button coordinate by name, adjusted for aspect ratio.

        Args:
            name: Button name (e.g. ``"pass"``, ``"keep"``).
            aspect: Window width/height ratio.  Defaults to 16:9 if None.

        Returns:
            Resolution-compensated ``ScreenCoord``, or None.
        """
        if aspect is None:
            aspect = cls._REF_ASPECT

        key = name.lower()

        # Map name aliases to canonical (vp_x, vp_y, description)
        entry = cls._resolve_button(key)
        if entry is None:
            return None

        vp_x, vp_y, desc = entry
        win_x, win_y = cls._to_window_coord(vp_x, vp_y, aspect)

        logger.debug(
            f"ButtonCoord '{key}': vp({vp_x:.3f},{vp_y:.3f}) "
            f"-> win({win_x:.3f},{win_y:.3f}) [aspect={aspect:.3f}]"
        )
        return ScreenCoord(win_x, win_y, desc)

    @classmethod
    def _resolve_button(
        cls, key: str
    ) -> Optional[tuple[float, float, str]]:
        """Resolve a button name to viewport-relative (x, y, description).

        The primary prompt button (Pass/Done/Submit/Resolve/etc.) all
        occupy the same ``_primaryButton`` position in the
        ``_buttonsLayout``.  The secondary button (Resolve-All, Cancel)
        sits to its left.
        """
        # Primary prompt button aliases
        if key in (
            "pass", "pass_turn", "resolve", "done",
            "next", "attack", "block", "no_attacks", "no_blocks",
        ):
            return (cls._PRIMARY_X_16_9, cls._PRIMARY_Y_16_9, f"{key.replace('_', ' ').title()} button")

        # Mulligan browser buttons
        if key == "keep":
            return (cls._KEEP_X_16_9, cls._KEEP_Y_16_9, "Keep button")
        if key == "mulligan":
            return (cls._MULL_X_16_9, cls._MULL_Y_16_9, "Mulligan button")

        # Scry browser buttons
        if key == "scry_top":
            return (cls._SCRY_TOP_X_16_9, cls._SCRY_TOP_Y_16_9, "Scry to Top")
        if key == "scry_bottom":
            return (cls._SCRY_BOT_X_16_9, cls._SCRY_BOT_Y_16_9, "Scry to Bottom")

        return None


# Backwards-compatible alias so existing imports keep working.
FixedCoordinates = ButtonCoordinates


class ScreenMapper:
    """Maps game actions to screen coordinates in the MTGA window."""

    def __init__(self):
        """Initialize the screen mapper."""
        self._window_rect: Optional[tuple[int, int, int, int]] = None
        self._hwnd: Optional[int] = None

    def get_mtga_window(self) -> Optional[tuple[int, int, int, int]]:
        """Find the MTGA window and return its client-area rectangle.

        Uses ctypes user32 (FindWindowW + GetClientRect + ClientToScreen)
        instead of pygetwindow for reliable detection that avoids title bar
        offset bugs and works without extra pip dependencies.

        Returns:
            (left, top, width, height) of the client area, or None if not found.
        """
        try:
            from arenamcp.input_controller import find_mtga_hwnd, get_client_rect

            hwnd = find_mtga_hwnd()
            if not hwnd:
                logger.warning("MTGA window not found")
                return None

            self._hwnd = hwnd
            rect = get_client_rect(hwnd)
            if not rect:
                logger.warning("Failed to get MTGA client rect")
                return None

            self._window_rect = rect
            return rect
        except Exception as e:
            logger.error(f"Failed to get MTGA window: {e}")
            return None

    @property
    def window_rect(self) -> Optional[tuple[int, int, int, int]]:
        """Cached window rectangle, refreshed on demand."""
        if self._window_rect is None:
            self.get_mtga_window()
        return self._window_rect

    def refresh_window(self) -> Optional[tuple[int, int, int, int]]:
        """Force refresh window position."""
        self._window_rect = None
        return self.get_mtga_window()

    def _current_aspect(self) -> float:
        """Return the current window aspect ratio, or 16:9 as default."""
        rect = self._window_rect
        if rect:
            _, _, w, h = rect
            if h > 0:
                return w / h
        return 16.0 / 9.0

    def get_button_coord(self, name: str) -> Optional[ScreenCoord]:
        """Get the screen coordinate for a known button.

        Coordinates are adjusted for the current window aspect ratio so
        that pillar-boxing (ultra-wide) and letter-boxing (4:3) are
        accounted for automatically.

        Args:
            name: Button name (e.g., "pass", "keep", "resolve").

        Returns:
            ScreenCoord or None if button not found.
        """
        return ButtonCoordinates.get(name, aspect=self._current_aspect())

    def _hand_arc_positions(self, hand_size: int) -> list[tuple[float, float]]:
        """Compute normalized (x, y) positions for each card index using MTGA arc layout.

        Based on MTGA CardLayout_Hand constants:
          Radius=45, FitAngle=30, MaxDeltaAngle=4.5, YOffset=-1.5

        The MTGA hand places cards on a circular arc.  Middle cards sit at the
        peak of the arc (highest on screen = lowest y value) while edge cards
        droop slightly downward.

        Returns:
            List of (norm_x, norm_y) tuples, one per card index.
        """
        if hand_size == 0:
            return []
        if hand_size == 1:
            return [(0.5, 0.95)]

        RADIUS = 45.0
        FIT_ANGLE = 30.0
        MAX_DELTA = 4.5
        Y_OFFSET = -1.5

        delta_angle = min(MAX_DELTA, FIT_ANGLE / (hand_size - 1))

        # Leftmost angle for arc base Y calculation
        leftmost_angle_deg = 90.0 + FIT_ANGLE * 0.5
        arc_base_y = math.sin(math.radians(leftmost_angle_deg)) * RADIUS

        # Starting angle for this hand size
        start_angle_deg = 90.0 + delta_angle * (hand_size - 1) * 0.5

        positions: list[tuple[float, float]] = []
        for i in range(hand_size):
            angle_deg = start_angle_deg - delta_angle * i
            angle_rad = math.radians(angle_deg)

            world_x = math.cos(angle_rad) * RADIUS
            world_y = math.sin(angle_rad) * RADIUS - arc_base_y + Y_OFFSET

            positions.append((world_x, world_y))

        # Convert world coords to normalized screen coords.
        # Determine x range from the full arc extent.
        full_left_x = math.cos(math.radians(90.0 + FIT_ANGLE * 0.5)) * RADIUS
        full_right_x = math.cos(math.radians(90.0 - FIT_ANGLE * 0.5)) * RADIUS
        x_range = full_right_x - full_left_x  # positive range

        # Screen mapping bounds
        SCREEN_HAND_LEFT = 0.20
        SCREEN_HAND_RIGHT = 0.80
        SCREEN_HAND_Y_CENTER = 0.95
        SCREEN_Y_SCALE = 0.003  # small y variation for arc droop

        normalized: list[tuple[float, float]] = []
        for wx, wy in positions:
            # Map world X to screen X
            norm_x = SCREEN_HAND_LEFT + (wx - full_left_x) / x_range * (
                SCREEN_HAND_RIGHT - SCREEN_HAND_LEFT
            )
            # Map world Y to screen Y (inverted: higher world y -> lower screen y)
            norm_y = SCREEN_HAND_Y_CENTER - wy * SCREEN_Y_SCALE

            # Clamp to safe screen region
            norm_x = max(0.15, min(0.85, norm_x))
            norm_y = max(0.88, min(0.98, norm_y))

            normalized.append((norm_x, norm_y))

        return normalized

    @staticmethod
    def _mtga_hand_sort_key(card: dict[str, Any]) -> tuple:
        """Sort key matching MTGA's hand card visual ordering.

        Matches MTGA's MulliganWorkflow.SortCards() ordering:
        1. Lands first (before non-lands)
        2. By first frame color (WUBRG order: W=1, U=2, B=3, R=4, G=5)
        3. By CMC ascending
        4. By card name alphabetically
        """
        # 1. Lands first (sort key 0) vs non-lands (sort key 1)
        card_types = card.get("card_types", [])
        type_line = card.get("type_line", "")
        is_land = any("Land" in ct for ct in card_types) or "Land" in type_line
        land_key = 0 if is_land else 1

        # 2. First frame color in WUBRG order
        # Parse from mana_cost string like "{2}{G}{G}" or "{1}{W}{U}"
        mana_cost = card.get("mana_cost", "")
        # CardColor enum: Colorless=0, W=1, U=2, B=3, R=4, G=5
        color_order = {"W": 1, "U": 2, "B": 3, "R": 4, "G": 5}
        first_color = 0  # Colorless default
        for ch in mana_cost:
            if ch in color_order:
                first_color = color_order[ch]
                break

        # 3. CMC — sum up mana cost components
        cmc = 0
        for part in re.findall(r'\{([^}]+)\}', mana_cost):
            if part.isdigit():
                cmc += int(part)
            elif part in color_order or part == "C":
                cmc += 1
            elif part == "X":
                pass  # X doesn't count toward CMC for sorting

        # 4. Card name
        name = card.get("name", "").lower()

        return (land_key, first_color, cmc, name)

    def get_card_in_hand_coord(
        self,
        card_name: str,
        hand_cards: list[dict[str, Any]],
        game_state: dict[str, Any],
    ) -> Optional[ScreenCoord]:
        """Calculate the screen position of a card in hand.

        Uses arc-based positioning matching MTGA's CardLayout_Hand
        circular fan layout.

        Cards are sorted by MTGA's visual ordering (lands first, then by
        color/CMC/name) before computing arc positions, since the game
        state zone order differs from the on-screen display order.

        Args:
            card_name: Name of the card to find.
            hand_cards: List of card dicts in hand.
            game_state: Full game state for additional context.

        Returns:
            ScreenCoord for the card, or None if not found.
        """
        if not hand_cards:
            return None

        # Sort hand to match MTGA's visual order (derived from RE of
        # MTGA hand sort: lands first, then color/CMC/name)
        sorted_hand = sorted(hand_cards, key=self._mtga_hand_sort_key)

        if logger.isEnabledFor(logging.DEBUG):
            gs_names = [c.get("name", "???") for c in hand_cards]
            vis_names = [c.get("name", "???") for c in sorted_hand]
            logger.debug(f"Hand sort: GS={gs_names} -> Visual={vis_names}")

        card_index = self._find_card_index(card_name, sorted_hand)

        if card_index is None:
            hand_names = [c.get("name", "???") for c in sorted_hand]
            logger.warning(
                f"Card '{card_name}' not found in hand: {hand_names}"
            )
            return None

        positions = self._hand_arc_positions(len(sorted_hand))
        x, y = positions[card_index]

        logger.info(
            f"Arc calc: card_idx={card_index}, size={len(sorted_hand)}, "
            f"x_norm={x:.3f}, y_norm={y:.3f} (visual sort order)"
        )
        return ScreenCoord(x, y, f"Hand card: {card_name}")

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize card name for matching.

        Handles: curly apostrophes, numbered duplicates (Swamp #2),
        leading/trailing whitespace, case.
        """
        n = name.lower().strip()
        # Normalize Unicode apostrophes/quotes to ASCII
        n = n.replace("\u2019", "'").replace("\u2018", "'")
        n = n.replace("\u201c", '"').replace("\u201d", '"')
        # Strip trailing "#N" duplicate markers (e.g., "Swamp #2" -> "swamp")
        n = re.sub(r'\s*#\d+$', '', n)
        return n

    def _find_card_index(
        self, card_name: str, hand_cards: list[dict[str, Any]]
    ) -> Optional[int]:
        """Find a card's index in hand using progressively fuzzier matching.

        Match order:
        1. Exact (case-insensitive)
        2. Normalized (strip punctuation variants, #N suffixes)
        3. Partial (card_name substring of hand name, or vice versa)
        4. Word overlap (share significant words)
        """
        target = card_name.lower().strip()
        target_norm = self._normalize_name(card_name)

        # 1. Exact match
        for i, card in enumerate(hand_cards):
            if card.get("name", "").lower().strip() == target:
                return i

        # 2. Normalized match
        for i, card in enumerate(hand_cards):
            if self._normalize_name(card.get("name", "")) == target_norm:
                return i

        # 3. Partial / substring match (either direction)
        for i, card in enumerate(hand_cards):
            hand_name = card.get("name", "").lower().strip()
            if target in hand_name or hand_name in target:
                return i

        # 4. Word overlap (handles "Adherent's Heirloom" vs "Adherent's Heirloom (ART)")
        target_words = set(target_norm.split()) - {"the", "of", "a", "an"}
        if target_words:
            best_score = 0
            best_idx = None
            for i, card in enumerate(hand_cards):
                card_words = set(self._normalize_name(card.get("name", "")).split()) - {"the", "of", "a", "an"}
                overlap = len(target_words & card_words)
                if overlap > best_score and overlap >= min(2, len(target_words)):
                    best_score = overlap
                    best_idx = i
            if best_idx is not None:
                return best_idx

        return None

    # -- Card type classification --
    # MTGA classifies permanents into four region types per player side.

    @staticmethod
    def _is_creature(card: dict[str, Any]) -> bool:
        """Check if a battlefield card is a creature."""
        card_types = card.get("card_types", [])
        if card_types:
            return any("Creature" in ct for ct in card_types)
        type_line = card.get("type_line", "")
        return "creature" in type_line.lower()

    @staticmethod
    def _is_land(card: dict[str, Any]) -> bool:
        """Check if a battlefield card is a land."""
        card_types = card.get("card_types", [])
        if card_types:
            return any("Land" in ct for ct in card_types)
        type_line = card.get("type_line", "")
        return "land" in type_line.lower()

    @staticmethod
    def _is_planeswalker_row(card: dict[str, Any]) -> bool:
        """Check if a card belongs in the planeswalker/saga row.

        From BattlefieldLayout_MP.GenerateData(): Planeswalkers, Sagas,
        Classes, Cases, and Rooms all go to the planeswalker region.
        """
        card_types = card.get("card_types", [])
        subtypes = card.get("subtypes", [])
        type_line = card.get("type_line", "").lower()

        # CardType checks
        if any("Planeswalker" in ct for ct in card_types):
            return True
        if any("Battle" in ct for ct in card_types):
            return True

        # SubType checks (Saga, Class, Case, Room)
        pw_subtypes = {"Saga", "Class", "Case", "Room"}
        if any(st in pw_subtypes for st in subtypes):
            return True

        # Fallback: check type_line string
        for keyword in ("planeswalker", "battle", "saga", "class", "case", "room"):
            if keyword in type_line:
                return True

        return False

    @staticmethod
    def _is_attached(card: dict[str, Any]) -> bool:
        """Check if a card is attached to another permanent (aura/equipment).

        Attached cards do not occupy their own position in the battlefield
        row -- they are rendered on top of their parent permanent.
        """
        # parent_instance_id in our game state maps to GRE's parentId,
        # which is set for attached auras, equipment, and abilities.
        parent_id = card.get("parent_instance_id")
        if parent_id and parent_id > 0:
            return True
        # Fallback: check type_line for Aura (catches cases where
        # parent_instance_id might not yet be populated in a diff)
        type_line = card.get("type_line", "").lower()
        return "aura" in type_line

    @staticmethod
    def _classify_region(card: dict[str, Any]) -> str:
        """Classify a card into its battlefield region.

        Region assignment order matches MTGA's BattlefieldLayout:
          1. Creature  -> creature region
          2. Planeswalker / Battle / Saga / Class / Case / Room
                       -> planeswalker region
          3. Land      -> land region
          4. Everything else (artifacts, enchantments) -> artifact region

        Returns one of: "creature", "planeswalker", "land", "artifact".
        """
        if ScreenMapper._is_creature(card):
            return "creature"
        if ScreenMapper._is_planeswalker_row(card):
            return "planeswalker"
        if ScreenMapper._is_land(card):
            return "land"
        return "artifact"

    # -- Battlefield row Y coordinates --
    # Derived from MTGA's viewport-space region anchors observed in the
    # UniversalBattlefieldRegion BoundsDefinition (AnchorMin/AnchorMax).
    #
    # MTGA layout from top to bottom (opponent then local player):
    #   Opponent lands         y ~ 0.14
    #   Opponent artifacts     y ~ 0.22
    #   Opponent planeswalkers y ~ 0.28
    #   Opponent creatures     y ~ 0.38
    #   --- center line ~0.48 ---
    #   Local creatures        y ~ 0.58
    #   Local planeswalkers    y ~ 0.66
    #   Local artifacts        y ~ 0.72
    #   Local lands            y ~ 0.78
    #
    # When a row is empty its space is not strictly reclaimed, but the
    # primary rows (creatures, lands) remain fixed.  The secondary rows
    # (planeswalker, artifact) sit between them.

    _ROW_Y: dict[tuple[bool, str], float] = {
        # (is_yours, region) -> normalized y
        (True,  "creature"):     0.58,
        (True,  "planeswalker"): 0.66,
        (True,  "artifact"):     0.72,
        (True,  "land"):         0.78,
        (False, "creature"):     0.38,
        (False, "planeswalker"): 0.28,
        (False, "artifact"):     0.22,
        (False, "land"):         0.14,
    }

    @staticmethod
    def _sort_by_age(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort cards by instance_id ascending (oldest first).

        MTGA's StackBlockerAndAgeComparer sorts battlefield stacks by age
        so that identical permanents group with oldest leftmost.  Instance
        IDs increase monotonically, so they serve as a reliable age proxy.
        """
        return sorted(cards, key=lambda c: c.get("instance_id", 0))

    def _battlefield_row_positions(
        self, num_in_row: int
    ) -> list[float]:
        """Compute center-aligned X positions for permanents in one row.

        Derived from UniversalBattlefieldGroup.GenerateLayoutInternal()
        and LayoutDataGenerator.GetRowStartingPosition() which center-align
        stacks within a region bounded by [0.10 .. 0.90] viewport X.

        The algorithm:
        - Each card occupies a *slot* whose width shrinks as the row grows.
        - For small counts, a fixed comfortable spacing is used.
        - For large counts, cards compress to fit the available width,
          mimicking how MTGA overlaps cards.

        Returns:
            List of normalized X positions, one per card.
        """
        if num_in_row == 0:
            return []
        if num_in_row == 1:
            return [0.50]

        # MTGA uses a bounded region (roughly 80% of screen width).
        # Cards are center-aligned with configurable inter-card gutter.
        BF_LEFT = 0.10
        BF_RIGHT = 0.90
        BF_WIDTH = BF_RIGHT - BF_LEFT

        # Card width in normalized coords (empirical: ~0.065 of screen).
        CARD_W = 0.065
        # Comfortable gutter between cards.
        GUTTER = 0.020

        # Total width the row would ideally occupy.
        ideal_width = num_in_row * CARD_W + (num_in_row - 1) * GUTTER

        if ideal_width <= BF_WIDTH:
            # Fits comfortably -- center the group.
            start_x = 0.50 - ideal_width / 2.0 + CARD_W / 2.0
            step = CARD_W + GUTTER
        else:
            # Compress: distribute card centers evenly across the region.
            # This mirrors MTGA's overlapping behaviour.
            start_x = BF_LEFT + CARD_W / 2.0
            usable = BF_WIDTH - CARD_W
            step = usable / (num_in_row - 1) if num_in_row > 1 else 0.0

        return [start_x + i * step for i in range(num_in_row)]

    def get_permanent_coord(
        self,
        card_name: str,
        instance_id: Optional[int],
        battlefield: list[dict[str, Any]],
        owner_seat: int,
        local_seat: int,
    ) -> Optional[ScreenCoord]:
        """Calculate the screen position of a permanent on the battlefield.

        Uses a four-region layout per player side matching MTGA's
        battlefield layout:

        Region assignment (checked in order):
          1. Creatures  -> creature row (closest to center line)
          2. Planeswalkers / Battles / Sagas / Classes / Cases / Rooms
                        -> planeswalker row
          3. Lands      -> land row (furthest from center line)
          4. Everything else (artifacts, enchantments) -> artifact row

        Cards attached to other permanents (auras, equipment with
        parentId set) are excluded from independent row positioning and
        are treated as overlaying their parent.

        Within each row, cards are sorted by instance_id (oldest first /
        leftmost), matching StackBlockerAndAgeComparer from the game.
        """
        is_yours = owner_seat == local_seat

        # Filter to the controller's permanents on the battlefield.
        owner_cards = [c for c in battlefield if c.get("owner_seat_id") == owner_seat]

        # Separate attached cards (auras / equipment) -- they overlay
        # their parent and do not take up row space.
        free_cards = [c for c in owner_cards if not self._is_attached(c)]
        attached_cards = [c for c in owner_cards if self._is_attached(c)]

        # Classify into the four MTGA regions.
        rows: dict[str, list[dict[str, Any]]] = {
            "creature": [],
            "planeswalker": [],
            "land": [],
            "artifact": [],
        }
        for card in free_cards:
            region = self._classify_region(card)
            rows[region].append(card)

        # Sort each row by age (oldest first = leftmost).
        for region in rows:
            rows[region] = self._sort_by_age(rows[region])

        if logger.isEnabledFor(logging.DEBUG):
            for region, cards in rows.items():
                names = [c.get("name", "?") for c in cards]
                if names:
                    logger.debug(f"Row '{region}' ({'yours' if is_yours else 'opp'}): {names}")

        # Locate the target card in the rows (or among attached cards).
        target_card = None
        card_region: Optional[str] = None
        card_idx_in_row: Optional[int] = None

        # Build a flat search list: all rows + attached cards.
        all_regions = list(rows.keys())

        def _search_rows(predicate):
            """Search rows with a predicate returning (card, region, idx) or None."""
            nonlocal target_card, card_region, card_idx_in_row
            for reg in all_regions:
                for i, card in enumerate(rows[reg]):
                    if predicate(card):
                        target_card = card
                        card_region = reg
                        card_idx_in_row = i
                        return True
            # Also check attached cards -- they get positioned at their
            # parent's location.
            for card in attached_cards:
                if predicate(card):
                    target_card = card
                    card_region = None  # signals "attached"
                    card_idx_in_row = None
                    return True
            return False

        # 1) Exact instance_id match.
        if instance_id and not _search_rows(
            lambda c: c.get("instance_id") == instance_id
        ):
            pass  # fall through

        # 2) Exact name match (case-insensitive).
        if target_card is None:
            _search_rows(
                lambda c: c.get("name", "").lower() == card_name.lower()
            )

        # 3) Partial / substring name match.
        if target_card is None:
            _search_rows(
                lambda c: card_name.lower() in c.get("name", "").lower()
            )

        if target_card is None:
            logger.warning(f"Permanent '{card_name}' not found on battlefield")
            return None

        # Handle attached cards: position at their parent's location.
        if card_region is None:
            parent_id = target_card.get("parent_instance_id")
            if parent_id:
                # Recurse: find the parent permanent's position.
                parent_name = None
                for c in battlefield:
                    if c.get("instance_id") == parent_id:
                        parent_name = c.get("name", card_name)
                        break
                if parent_name:
                    logger.info(
                        f"'{card_name}' is attached to '{parent_name}' "
                        f"(id={parent_id}), using parent position"
                    )
                    return self.get_permanent_coord(
                        parent_name, parent_id, battlefield,
                        owner_seat, local_seat
                    )
            # Fallback: if parent not found, place at creature row center.
            y = self._ROW_Y.get((is_yours, "creature"), 0.58 if is_yours else 0.38)
            return ScreenCoord(0.50, y, f"Permanent (attached): {card_name}")

        # Look up the Y coordinate for this region/side.
        y = self._ROW_Y[(is_yours, card_region)]

        # Compute X positions for the row.
        row_cards = rows[card_region]
        xs = self._battlefield_row_positions(len(row_cards))
        x = xs[card_idx_in_row]

        logger.info(
            f"Mapped '{card_name}' -> ({x:.3f}, {y:.3f}) "
            f"[{card_region}, idx={card_idx_in_row}/{len(row_cards)}, "
            f"{'yours' if is_yours else 'opponent'}]"
        )

        return ScreenCoord(x, y, f"Permanent: {card_name}")

    def get_card_coord_via_vision(
        self, card_name: str, screenshot_bytes: bytes, backend: Any
    ) -> Optional[ScreenCoord]:
        """Use vision LLM to locate a card on screen.

        Fallback for when positional heuristics fail.
        """
        logger.info(f"Using Vision to locate '{card_name}'...")
        
        system_prompt = """You are an MTG Arena UI locator. 
        Analyze the screenshot and find the EXACT center of the card or button requested.
        Output ONLY a JSON object with 'x' and 'y' as normalized coordinates (0.0 to 1.0).
        Example: {"x": 0.45, "y": 0.58}
        """
        
        user_msg = f"Find the card named '{card_name}' on the battlefield or in hand."
        
        try:
            if not hasattr(backend, 'complete_with_image'):
                logger.warning("Current backend does not support vision.")
                return None
                
            response = backend.complete_with_image(system_prompt, user_msg, screenshot_bytes)
            
            # Extract JSON from response
            import json
            import re
            match = re.search(r'\{.*\}', response)
            if match:
                data = json.loads(match.group(0))
                vx = data.get("x")
                vy = data.get("y")
                if vx is not None and vy is not None:
                    logger.info(f"Vision found '{card_name}' at ({vx}, {vy})")
                    return ScreenCoord(vx, vy, f"Vision: {card_name}")
        except Exception as e:
            logger.error(f"Vision detection failed: {e}")
            
        return None

    def get_option_coord(
        self, option_index: int, total_options: int, context: str = ""
    ) -> Optional[ScreenCoord]:
        """Calculate position for modal/select UI options.

        MTGA presents options in a centered vertical list or horizontal row.

        Args:
            option_index: 0-based index of the option to click.
            total_options: Total number of options presented.
            context: Optional context hint (e.g., "modal", "scry").

        Returns:
            ScreenCoord for the option.
        """
        if total_options <= 0:
            return None

        # Options are typically centered, stacked vertically
        # Roughly y=0.40 to y=0.60, centered at x=0.50
        option_top = 0.40
        option_bottom = 0.60

        if total_options == 1:
            y = 0.50
        else:
            spacing = (option_bottom - option_top) / (total_options - 1)
            y = option_top + option_index * spacing

        return ScreenCoord(0.50, y, f"Option {option_index + 1}/{total_options}")

    def get_draft_card_coord(
        self, card_index: int, pack_size: int
    ) -> Optional[ScreenCoord]:
        """Calculate position of a card in a draft pack.

        Draft packs are displayed in a grid, typically 3 rows of 5.

        Args:
            card_index: 0-based index in the pack.
            pack_size: Number of cards in the pack.

        Returns:
            ScreenCoord for the card.
        """
        if pack_size <= 0:
            return None

        # Draft grid: roughly 5 columns, up to 3 rows
        cols = min(pack_size, 5)
        rows = (pack_size + cols - 1) // cols

        row = card_index // cols
        col = card_index % cols

        # Grid spans x=0.15 to x=0.85, y=0.20 to y=0.70
        grid_left = 0.15
        grid_right = 0.85
        grid_top = 0.20
        grid_bottom = 0.70

        if cols == 1:
            x = 0.50
        else:
            x = grid_left + col * (grid_right - grid_left) / (cols - 1)

        if rows == 1:
            y = 0.45
        else:
            y = grid_top + row * (grid_bottom - grid_top) / (rows - 1)

        return ScreenCoord(x, y, f"Draft card {card_index + 1}/{pack_size}")
