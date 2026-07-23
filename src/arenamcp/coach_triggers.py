"""Game-state trigger detection for the coach engine.

Extracted from arenamcp.coach (pure move, no behavior change).
Re-exported from arenamcp.coach for backwards compatibility."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GameStateTrigger:
    """Detects trigger conditions by comparing game states."""

    # Tier list of dangerous cards that warrant immediate warning
    # Format: card_name -> brief description of the threat
    THREAT_CARDS = {
        # Board wipes
        "Wrath of God": "Board wipe! Destroys all creatures.",
        "Damnation": "Board wipe! Destroys all creatures.",
        "Farewell": "Exiles ALL permanents of chosen types!",
        "Sunfall": "Exiles all creatures, makes a big token.",
        "Depopulate": "Board wipe, draws if you have multicolor.",
        "Temporary Lockdown": "Exiles all permanents MV 2 or less!",
        "Meticulous Archive": "Can find board wipes or removal.",
        # Combo pieces / Must-answer threats
        "Sheoldred, the Apocalypse": "Drains 2 on your draws, heals on theirs!",
        "Atraxa, Grand Unifier": "Draws 10+ cards on ETB, lifelink flyer.",
        "Raffine, Scheming Seer": "Grows attackers and filters cards.",
        "The Wandering Emperor": "Flash! Can exile or make blockers anytime.",
        "Teferi, Time Raveler": "Shuts off your instant-speed plays!",
        "Narset, Parter of Veils": "You can only draw 1 card per turn!",
        "Omnath, Locus of Creation": "Massive value engine, gains life.",
        "Vorinclex, Voice of Hunger": "Doubles their counters, halves yours.",
        # Powerful planeswalkers
        "Oko, Thief of Crowns": "Elks your best creatures!",
        "Karn, the Great Creator": "Shuts off artifacts, grabs from sideboard.",
        "Wrenn and Six": "Recurring lands and pinging creatures.",
        # Lock pieces
        "Drannith Magistrate": "You can't cast from graveyard/exile!",
        "Archon of Emeria": "Only 1 spell per turn, lands ETB tapped.",
        "Thalia, Guardian of Thraben": "Noncreature spells cost 1 more.",
        "Authority of the Consuls": "Your creatures ETB tapped.",
        "High Noon": "Only 1 spell per turn for everyone.",
        # Removal magnets
        "Questing Beast": "Can't be chumped, damages walkers!",
        "Elder Gargaroth": "Massive value every combat.",
        "Cruelty of Gix": "3-mode saga, steals creatures!",
        # Enchantment threats
        "Monument to Endurance": "Grows huge with counters, gains deathtouch + indestructible!",
    }

    def __init__(self, life_threshold: int = 5):
        """Initialize trigger detector.

        Args:
            life_threshold: Life total below which "low_life" triggers (default: 5)
        """
        self.life_threshold = life_threshold
        # Track threats we've already warned about (by instance_id)
        self._seen_threats: set[int] = set()
        # Track whether we've already fired the losing_badly trigger this game
        self._losing_badly_fired = False
        self._last_threat: dict | None = None

    def _get_local_player(self, state: dict[str, Any]) -> dict | None:
        """Get the local player dict from game state."""
        for p in state.get("players", []):
            if p.get("is_local"):
                return p
        return None

    def _get_opponent_player(self, state: dict[str, Any]) -> dict | None:
        """Get the opponent player dict from game state."""
        for p in state.get("players", []):
            if not p.get("is_local"):
                return p
        return None

    def _has_castable_instants(self, state: dict[str, Any]) -> bool:
        """Check if player has any instant-speed cards they can cast.

        Returns True if hand contains instants or flash cards that can be
        cast with the current available mana.
        """
        import re

        # Count untapped lands for mana
        local_seat = None
        for p in state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

        if local_seat is None:
            return False

        battlefield = state.get("battlefield", [])
        untapped_lands = sum(
            1
            for c in battlefield
            if c.get("owner_seat_id") == local_seat
            and "land" in c.get("type_line", "").lower()
            and not c.get("is_tapped")
        )

        # Check hand for castable instants/flash
        hand = state.get("hand", [])
        for card in hand:
            type_line = card.get("type_line", "").lower()
            oracle_text = card.get("oracle_text", "").lower()

            # Check if instant speed
            is_instant_speed = "instant" in type_line or "flash" in oracle_text
            if not is_instant_speed:
                continue

            # Calculate CMC
            cost = card.get("mana_cost", "")
            cmc = 0
            if cost:
                generic = re.findall(r"\{(\d+)\}", cost)
                cmc += sum(int(g) for g in generic)
                colored = re.findall(r"\{[WUBRGC]\}", cost)
                cmc += len(colored)
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                cmc += len(hybrid)

            if untapped_lands >= cmc:
                return True

        return False

    def check_triggers(self, prev_state: dict[str, Any], curr_state: dict[str, Any]) -> list[str]:
        """Compare two game states and return triggered condition names.

        Args:
            prev_state: Previous game state dict
            curr_state: Current game state dict

        Returns:
            List of trigger names that fired (may be empty)
        """
        triggers = []

        prev_turn = prev_state.get("turn", {})
        curr_turn = curr_state.get("turn", {})

        # Retrieve phase and step early (fix scoping issues)
        curr_phase = curr_turn.get("phase", "")
        curr_step = curr_turn.get("step", "")

        # Get local player info first (needed for turn detection)
        prev_local = self._get_local_player(prev_state)
        curr_local = self._get_local_player(curr_state)
        local_seat = curr_local.get("seat_id") if curr_local else None

        # FIRST CONNECTION: If prev_state has no turn info but curr_state does,
        # we just connected mid-game. Fire a trigger to give immediate advice.
        prev_turn_num = prev_turn.get("turn_number", 0)
        curr_turn_num = curr_turn.get("turn_number", 0)
        curr_active = curr_turn.get("active_player", 0)

        first_connection = prev_turn_num == 0 and curr_turn_num > 0
        if first_connection:
            # Just connected to an active game
            is_your_turn = curr_active == local_seat
            if is_your_turn:
                logger.info(f"First connection mid-game, triggering new_turn (turn {curr_turn_num})")
                triggers.append("new_turn")
            # Also check for pending decision on first connection
            pending = curr_state.get("pending_decision")
            if pending:
                logger.info(f"First connection with pending decision: {pending}")
                triggers.append("decision_required")

        # New turn detection. Skip on first connection — that path above already
        # owns the new_turn decision (gated on whether it's the local player's
        # turn); without this guard a first-connection-on-your-turn fires
        # new_turn twice for the same turn.
        if curr_turn_num > prev_turn_num and not first_connection:
            triggers.append("new_turn")

        # Check if it's your turn or opponent's turn
        is_your_turn = curr_active == local_seat

        # Priority gained - trigger when priority shifts to you
        prev_priority = prev_turn.get("priority_player", 0)
        curr_priority = curr_turn.get("priority_player", 0)
        if local_seat and curr_priority == local_seat and prev_priority != local_seat:
            # Always trigger on your turn
            # On opponent's turn, trigger if:
            #   1. You have castable instants
            #   2. There's something on the stack to consider
            #   3. We're in a significant phase (combat, main)
            has_options = self._has_castable_instants(curr_state)
            has_stack = len(curr_state.get("stack", [])) > 0
            # Retrieve phase and step early
            curr_phase = curr_turn.get("phase", "")
            curr_step = curr_turn.get("step", "")

            if (
                is_your_turn
                or has_options
                or has_stack
                or (any(p in curr_phase for p in ["Main", "Combat", "Beginning"]))
            ):
                triggers.append("priority_gained")

        # --- Detect land_played and spell_resolved EARLY ---
        # These must run before the legal_actions decision_required check
        # so the suppression at line ~3445 can see them and avoid firing
        # a duplicate decision_required that contradicts multi-step advice.
        prev_stack = prev_state.get("stack", [])
        curr_stack = curr_state.get("stack", [])

        # Land played detection - only on your turn, only in main phases
        if is_your_turn and "Main" in curr_phase:
            prev_battlefield = prev_state.get("battlefield", [])
            curr_battlefield = curr_state.get("battlefield", [])

            prev_land_count = sum(
                1
                for obj in prev_battlefield
                if obj.get("owner_seat_id") == local_seat and "land" in obj.get("type_line", "").lower()
            )
            curr_land_count = sum(
                1
                for obj in curr_battlefield
                if obj.get("owner_seat_id") == local_seat and "land" in obj.get("type_line", "").lower()
            )

            if curr_land_count > prev_land_count:
                logger.info(f"Land played trigger: {prev_land_count} -> {curr_land_count}")
                triggers.append("land_played")

        # Spell resolved detection - your spell left the stack on your turn
        if is_your_turn and len(curr_stack) < len(prev_stack):
            prev_your_spells = [s for s in prev_stack if s.get("owner_seat_id") == local_seat]
            curr_your_spells = [s for s in curr_stack if s.get("owner_seat_id") == local_seat]
            if len(curr_your_spells) < len(prev_your_spells):
                logger.info("Spell resolved trigger: your spell left the stack")
                triggers.append("spell_resolved")

            if len(curr_stack) == 0 and "spell_resolved" not in triggers and "Main" in curr_phase:
                logger.info("Stack cleared trigger: opponent spell/ability resolved on your main phase")
                triggers.append("spell_resolved")

        # Check explicit pending decisions (like Mulligan) or legal action changes
        pending_decision = curr_state.get("pending_decision")
        legal_actions = curr_state.get("legal_actions", [])
        prev_legal = prev_state.get("legal_actions", [])

        # Trigger if decision label changed OR if we got a new list of legal actions from GRE
        if pending_decision and pending_decision != prev_state.get("pending_decision"):
            logger.info(f"Triggering decision: {pending_decision}")
            triggers.append("decision_required")
        elif legal_actions and legal_actions != prev_legal:
            # Don't re-trigger decision_required when the legal actions changed
            # because we just played a land or resolved a spell — those have
            # their own triggers and we already gave advice for the turn.
            if (
                "decision_required" not in triggers
                and "land_played" not in triggers
                and "spell_resolved" not in triggers
            ):
                logger.info(f"Triggering decision due to legal_actions update: {legal_actions}")
                triggers.append("decision_required")
        elif pending_decision in ("Mulligan", "Mulligan Bottom"):
            # Mulligan re-fire cases:
            # 1. Hand wasn't populated yet (SubmitDeckReq before GameState)
            # 2. Player chose to mulligan → new hand dealt (same decision
            #    label "Mulligan" but different hand contents/count)
            prev_hand = prev_state.get("hand", [])
            curr_hand = curr_state.get("hand", [])
            prev_hand_ids = {c.get("instance_id") for c in prev_hand}
            curr_hand_ids = {c.get("instance_id") for c in curr_hand}
            hand_changed = curr_hand_ids != prev_hand_ids
            if curr_hand and (not prev_hand or hand_changed):
                logger.info(
                    f"Re-triggering Mulligan decision "
                    f"(hand {'appeared' if not prev_hand else 'changed'}: "
                    f"{len(curr_hand)} cards)"
                )
                triggers.append("decision_required")

        # Combat phase detection - use pending steps to catch fast combat phases
        pending_steps = curr_turn.get("pending_combat_steps", [])

        # P2-10: queued steps drain one poll late — once the snapshot phase
        # has moved PAST combat (Main2/Ending) the trigger's advice is
        # unsatisfiable before the LLM call is even made (2 guaranteed-wasted
        # calls, 4.7s + 1.9s, on 2026-07-05).
        if pending_steps and any(marker in curr_phase for marker in ("Main2", "Second", "Ending", "End")):
            logger.debug(f"Dropping {len(pending_steps)} queued combat step(s) — phase already {curr_phase}")
            pending_steps = []

        for step_info in pending_steps:
            step = step_info.get("step", "")
            step_active = step_info.get("active_player", 0)
            step_is_your_turn = step_active == local_seat

            logger.debug(
                f"Processing pending combat step: {step}, active={step_active}, step_is_your_turn={step_is_your_turn}, current_is_your_turn={is_your_turn}"
            )

            # Double-check both the step's active player AND current turn state
            # This prevents stale pending steps from firing triggers after turn changes
            if "DeclareAttack" in step and step_is_your_turn and is_your_turn:
                if "combat_attackers" not in triggers:
                    logger.info(f"Combat attackers trigger from pending: {step}")
                    triggers.append("combat_attackers")
            elif "DeclareBlock" in step and not step_is_your_turn and not is_your_turn:
                if "combat_blockers" not in triggers:
                    logger.info(f"Combat blockers trigger from pending: {step}")
                    triggers.append("combat_blockers")

        # Also check current step (in case we're still in combat)
        # curr_phase and curr_step are already defined above

        if "Combat" in curr_phase:
            prev_step = prev_turn.get("step", "")
            # Only trigger on STEP CHANGE to avoid spamming every polling cycle
            if curr_step != prev_step:
                if "DeclareAttack" in curr_step and is_your_turn and "combat_attackers" not in triggers:
                    logger.info(f"Combat attackers trigger: step={curr_step}")
                    triggers.append("combat_attackers")
                elif "DeclareBlock" in curr_step and not is_your_turn and "combat_blockers" not in triggers:
                    logger.info(f"Combat blockers trigger: step={curr_step}")
                    triggers.append("combat_blockers")

        # Low life detection - always important
        if curr_local:
            curr_life = curr_local.get("life_total", 20)
            prev_life = prev_local.get("life_total", 20) if prev_local else 20
            if curr_life < self.life_threshold and prev_life >= self.life_threshold:
                triggers.append("low_life")

        # Opponent low life detection - always important
        prev_opp = self._get_opponent_player(prev_state)
        curr_opp = self._get_opponent_player(curr_state)
        if curr_opp:
            curr_opp_life = curr_opp.get("life_total", 20)
            prev_opp_life = prev_opp.get("life_total", 20) if prev_opp else 20
            if curr_opp_life < self.life_threshold and prev_opp_life >= self.life_threshold:
                triggers.append("opponent_low_life")

        # Stack spell detection - differentiate between your spells and opponent's
        if len(curr_stack) > len(prev_stack):
            # Check who owns the newest spell on the stack
            newest_spell = curr_stack[-1] if curr_stack else None
            if newest_spell:
                spell_owner = newest_spell.get("owner_seat_id")
                if spell_owner == local_seat:
                    triggers.append("stack_spell_yours")
                else:
                    triggers.append("stack_spell_opponent")

        # NOTE: land_played and spell_resolved are detected earlier (before
        # the legal_actions check) so that decision_required suppression works.

        # THREAT DETECTION - warn about dangerous opponent cards
        opp_seat = curr_opp.get("seat_id") if curr_opp else None
        if opp_seat:
            curr_battlefield = curr_state.get("battlefield", [])
            for card in curr_battlefield:
                # Only check opponent's permanents
                controller = card.get("controller_seat_id") or card.get("owner_seat_id")
                if controller != opp_seat:
                    continue

                instance_id = card.get("instance_id")
                card_name = card.get("name", "")

                # Check if this is a threat card we haven't warned about
                if card_name in self.THREAT_CARDS and instance_id not in self._seen_threats:
                    self._seen_threats.add(instance_id)
                    # Store threat info for the standalone coach to retrieve
                    self._last_threat = {
                        "name": card_name,
                        "warning": self.THREAT_CARDS[card_name],
                        "card": {
                            "name": card.get("name"),
                            "type_line": card.get("type_line"),
                            "oracle_text": card.get("oracle_text"),
                            "power": card.get("power"),
                            "toughness": card.get("toughness"),
                            "mana_cost": card.get("mana_cost"),
                            "counters": card.get("counters"),
                        },
                    }
                    logger.info(f"Threat detected: {card_name} - {self.THREAT_CARDS[card_name]}")
                    triggers.append("threat_detected")

                # Generic planeswalker detection fallback
                elif (
                    card_name not in self.THREAT_CARDS
                    and "planeswalker" in card.get("type_line", "").lower()
                    and instance_id not in self._seen_threats
                ):
                    self._seen_threats.add(instance_id)
                    self._last_threat = {
                        "name": card_name,
                        "warning": f"Opponent played planeswalker {card_name} — generates value every turn, consider attacking it.",
                        "card": {
                            "name": card.get("name"),
                            "type_line": card.get("type_line"),
                            "oracle_text": card.get("oracle_text"),
                            "power": card.get("power"),
                            "toughness": card.get("toughness"),
                            "mana_cost": card.get("mana_cost"),
                            "counters": card.get("counters"),
                        },
                    }
                    logger.info(f"Threat detected (planeswalker): {card_name}")
                    triggers.append("threat_detected")

        # LOSING BADLY detection — proactive concede suggestion
        # Fires once per game when multiple signals indicate a hopeless position.
        # Only check on new turns to avoid spamming during combat math.
        # Skip entirely if the match has ended or the bridge reports an
        # intermission state — otherwise a resumed coach process will
        # re-trigger losing_badly against post-match state and fire the
        # win-probability LLM call well after the user lost.
        pending_decision = str(curr_state.get("pending_decision") or "")
        bridge_req_type = str(curr_state.get("_bridge_request_type") or "")
        # _bridge_in_intermission is the durable signal — the bridge
        # zeroes _bridge_request_type when it sees an Intermission request,
        # so checking startswith("Intermission") on its own is dead code.
        match_in_intermission = (
            pending_decision.lower() == "intermission"
            or bridge_req_type.startswith("Intermission")
            or bool(curr_state.get("_bridge_in_intermission"))
            or bool(curr_state.get("match_ended"))
        )

        if (
            not self._losing_badly_fired
            and not match_in_intermission
            and curr_local
            and curr_opp
            and curr_turn_num >= 4  # too early to judge before turn 4
            and "new_turn" in triggers
        ):
            your_life = curr_local.get("life_total", 20)
            opp_life = curr_opp.get("life_total", 20)
            curr_bf = curr_state.get("battlefield", [])
            your_creatures = [
                c
                for c in curr_bf
                if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local_seat
                and c.get("power") is not None
                and "land" not in c.get("type_line", "").lower()
            ]
            opp_creatures = [
                c
                for c in curr_bf
                if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local_seat
                and c.get("power") is not None
                and "land" not in c.get("type_line", "").lower()
            ]
            your_power = sum(c.get("power") or 0 for c in your_creatures)
            opp_power = sum(c.get("power") or 0 for c in opp_creatures)
            hand_size = len(curr_state.get("hand", []))

            # Heuristic: multiple bad signals stacking up
            signals = 0
            if your_life <= 5:
                signals += 2
            elif your_life <= 10 and opp_life >= 15:
                signals += 1
            if opp_power >= your_life:  # opponent can lethal us
                signals += 2
            if len(opp_creatures) >= len(your_creatures) + 3:
                signals += 1
            if opp_power >= your_power + 8:
                signals += 1
            if hand_size == 0 and len(your_creatures) <= 1:
                signals += 1
            if your_life <= 3 and opp_power > 0:
                signals += 2  # almost certainly dead

            if signals >= 4:
                self._losing_badly_fired = True
                triggers.append("losing_badly")
                logger.info(
                    f"Losing badly detected: life={your_life} vs {opp_life}, "
                    f"power={your_power} vs {opp_power}, "
                    f"creatures={len(your_creatures)} vs {len(opp_creatures)}, "
                    f"hand={hand_size}, signals={signals}"
                )

        # Reset losing_badly flag on new game (turn resets to 0/1)
        if curr_turn_num <= 1 and prev_turn_num > 1:
            self._losing_badly_fired = False

        return triggers
