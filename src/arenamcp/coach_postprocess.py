"""Advice post-processing helpers for CoachEngine, extracted from coach.py.

Pure move: methods are unchanged and mixed back into CoachEngine."""

import contextlib
import logging
from collections import Counter
from typing import Any

from arenamcp.backend_health import (
    BACKEND_ERROR_PREFIX,
    LOCAL_FALLBACK_PREFIX,
    is_backend_error_text,
)
from arenamcp.coach_prompt_utils import (
    _NON_PASSABLE_REQUEST_CLASSES,
    _NON_PASSABLE_REQUEST_TYPES,
    _fallback_non_action_advice,
)

logger = logging.getLogger(__name__)

# Backend-error wins over local-fallback when both somehow appear.
_HEALTH_TAG_PRECEDENCE = (BACKEND_ERROR_PREFIX, LOCAL_FALLBACK_PREFIX)


def _normalize_health_tags(text: str, *, force_local_fallback: bool = False) -> str:
    """Hoist any health tag to the front of ``text`` (exactly once).

    Two problems this solves:

    1. Callers prepend framing to advice ("<game plan intro>. <advice>"),
       which buries a leading ``[LOCAL FALLBACK]`` mid-string. ``strip_health_tags``
       is ``startswith``-based, so a buried tag is never removed and TTS reads
       "local fallback" out loud.
    2. Fallback advice generated inside this module needs the tag applied
       *after* the spoken-form rewrites below, which are anchored regexes
       (``^Done \\(confirm attackers\\)$``) that a prefix would break.

    ``force_local_fallback`` tags locally generated advice that carried no tag.
    """
    if not text:
        return text
    import re as _re

    tag = ""
    for prefix in _HEALTH_TAG_PRECEDENCE:
        if prefix in text:
            if not tag:
                tag = prefix
            text = text.replace(prefix, " ")
    text = _re.sub(r"\s+", " ", text).strip()
    # Framing prepended before a tag can leave a dangling separator
    # ("Plan: race. . pass priority") once the tag is lifted out.
    text = _re.sub(r"^[.,;:]+\s*", "", text).strip()
    if not tag and force_local_fallback:
        tag = LOCAL_FALLBACK_PREFIX
    if not tag:
        return text
    return f"{tag} {text}".strip()


class _AdvicePostprocessMixin:
    def _postprocess_advice(self, advice: str, game_state: dict[str, Any], style: str = "quick") -> str:
        """Post-process LLM advice to fix common issues with smaller models.

        1. Strip markdown formatting (headers, bold, bullets) for spoken output
        2. Truncate overly long responses when style is concise
        3. Remove 'Play [Land]' suggestions when no land is in hand
        4. Fix typos in card names using fuzzy matching against the game state

        This is a band-aid layer over freeform LLM prose. The cleaner long-term
        fix is to switch the coach to structured JSON output (action + say
        fields) the way the autopilot planner does — that way the action gets
        validated against legal actions before TTS reads it, and most of these
        regex passes go away. Tracked separately; for now we keep the cleanup
        targeted and data-driven rather than hardcoded.
        """
        if not advice:
            return ""

        import re

        # Initialize legal_actions early from game_state for early validation passes
        legal_actions = game_state.get("legal_actions") or []

        # Set when this method substitutes locally generated advice for the
        # model's. The tag is applied at the very end (see
        # ``_normalize_health_tags``) so the spoken-form rewrites below —
        # anchored regexes like ``^Done \(confirm attackers\)$`` — still match.
        local_fallback_used = False

        # 0a. Strip markdown formatting — this is spoken aloud, not rendered
        # Remove headers (# Header or ##Header — with or without space)
        advice = re.sub(r"^#{1,6}\s*", "", advice, flags=re.MULTILINE)
        # Remove bold/italic markers
        advice = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", advice)
        # Remove bullet markers at start of line (•, -, *)
        advice = re.sub(r"^\s*[•\-\*]\s+", "", advice, flags=re.MULTILINE)
        # Remove inline bullet characters (•)
        advice = advice.replace("•", "")
        # Collapse multiple newlines into single space
        advice = re.sub(r"\n+", " ", advice)
        # Clean up resulting whitespace
        advice = re.sub(r"\s+", " ", advice).strip()

        # 0b. Enforce style-specific length limits.
        # Quick: under 20 words (LLM sometimes ignores the prompt).
        # Chatty: under ~80 words so TTS stays under ~25 seconds.
        # Legacy names accepted: concise==quick, verbose==chatty.
        style_norm = (style or "").lower()
        if style_norm in ("quick", "concise"):
            word_cap, sent_cap = 22, 2
        elif style_norm in ("chatty", "verbose"):
            word_cap, sent_cap = 80, 5
        else:
            word_cap, sent_cap = 60, 4  # normal/default

        words = advice.split()
        if len(words) > word_cap + 5:  # small slack before truncating
            sentences = re.split(r"(?<=[.!?])\s+", advice)
            truncated = []
            count = 0
            for sent in sentences[:sent_cap]:
                sw = sent.split()
                if count + len(sw) > word_cap and truncated:
                    break
                truncated.append(sent)
                count += len(sw)
            advice = " ".join(truncated).strip()
            if advice and advice[-1] not in ".!?":
                advice += "."

        def _combat_attack_summary() -> tuple[int, int, int] | None:
            """Return (attack_power, opp_life, opp_blockers) if computable."""
            turn = game_state.get("turn", {})
            turn_num = turn.get("turn_number", 0)
            phase = turn.get("phase", "")

            players = game_state.get("players", [])
            local_player = next((p for p in players if p.get("is_local")), None)
            if not local_player:
                return None
            local_seat = local_player.get("seat_id")
            opponent_player = next((p for p in players if p.get("seat_id") != local_seat), None)
            if not opponent_player:
                return None

            if turn.get("active_player") != local_seat:
                return None
            if "Main" not in phase and "Combat" not in phase:
                return None

            battlefield = game_state.get("battlefield", [])
            your_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") == local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]

            def _has_haste(card: dict[str, Any]) -> bool:
                return "haste" in self._remove_reminder_text(card.get("oracle_text", "")).lower()

            valid_attackers = [
                c
                for c in your_creatures
                if not c.get("is_tapped")
                and not (c.get("turn_entered_battlefield") == turn_num and not _has_haste(c))
            ]
            attack_power = sum(c.get("power") or 0 for c in valid_attackers)

            opp_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") != local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]
            opp_blockers = len([c for c in opp_creatures if not c.get("is_tapped")])
            opp_life = opponent_player.get("life_total", 20)

            return attack_power, opp_life, opp_blockers

        # Get cards in hand
        hand_cards = game_state.get("hand", [])
        {c.get("name", "").lower() for c in hand_cards}

        # Get all card names in game state for fuzzy matching
        all_cards = []
        for zone in ["hand", "battlefield", "graveyard", "stack", "exile"]:
            all_cards.extend(game_state.get(zone, []))
        all_card_names = {c.get("name", "") for c in all_cards if c.get("name")}

        # 0. Resolve any raw Card<ID> or Card#<ID> patterns in advice using game_state and card_db
        raw_card_pattern = re.compile(r"\bCard#?(\d+)\b", re.IGNORECASE)
        if raw_card_pattern.search(advice):
            def _replace_raw_card_id(m):
                cid = int(m.group(1))
                for card_obj in all_cards:
                    if card_obj.get("instance_id") == cid or card_obj.get("grp_id") == cid:
                        cname = card_obj.get("name")
                        if cname and not cname.startswith("Card"):
                            return cname
                from arenamcp.rules_engine import RulesEngine
                db_info = RulesEngine._card_db.get(cid)
                if isinstance(db_info, dict) and db_info.get("name"):
                    return db_info["name"]
                return m.group(0)

            advice = raw_card_pattern.sub(_replace_raw_card_id, advice)

        # Build the set of land card names known to this game. Anything with
        # "Land" in its type_line counts — covers basics, snow, Triomes,
        # shocks, fetch, Cavern of Souls, etc. The hardcoded basic-only list
        # we used to keep here missed every modern land.
        known_land_names = {
            (c.get("name") or "").lower()
            for c in all_cards
            if "land" in (c.get("type_line") or "").lower() and c.get("name")
        }
        # Generic words the LLM might use even when no specific land card is
        # actually visible. Kept here (not in a regex literal) so the set is
        # easy to extend.
        generic_land_terms = {"a land", "any land", "land"}

        # Check whether any land sits in the local player's hand.
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        lands_in_hand: set[str] = set()
        for c in hand_cards:
            type_line = (c.get("type_line") or "").lower()
            name = (c.get("name") or "").lower()
            if "land" in type_line and name:
                lands_in_hand.add(name)

        # 1. Remove "Play <X>" suggestions when there's no land in hand.
        # We match X against actual land card names from this game's state
        # plus a small set of generic land phrases. This is data-driven
        # instead of a hardcoded basic-lands list, so Triomes / shocks /
        # snow basics all get caught too.
        if not lands_in_hand:
            removable_land_names = known_land_names | generic_land_terms
            if removable_land_names:
                # Sort longest first so multi-word names match before substrings
                land_alternatives = sorted(removable_land_names, key=len, reverse=True)
                land_pattern = re.compile(
                    r"Play\s+(?:" + "|".join(re.escape(n) for n in land_alternatives) + r")[.,]?\s*",
                    flags=re.IGNORECASE,
                )
                advice = land_pattern.sub("", advice)
                # Clean up any resulting double spaces or leading/trailing spaces
                advice = re.sub(r"\s+", " ", advice).strip()

        # 2. Fix typos in card names by fuzzy-matching against the actual
        # card names in this game's state. The previous hardcoded
        # typo-fixes dict (Gemma 3N misreads of specific card names) is
        # gone — it never scaled past the handful of cards we'd seen
        # break, and the fuzzy pass below catches the same misspellings
        # generically as long as the real card is somewhere in state.
        # Split advice into words and check for near-matches
        for card_name in all_card_names:
            if len(card_name) < 4:
                continue  # Skip short names to avoid false matches
            # Check if card name appears with typos (simple Levenshtein-like check)
            card_words = card_name.lower().split()
            for word in card_words:
                if len(word) < 4:
                    continue
                # Look for similar words in advice
                advice_words = advice.lower().split()
                for i, advice_word in enumerate(advice_words):
                    if len(advice_word) >= 4 and self._is_similar(word, advice_word):
                        # Replace the typo with correct spelling
                        # Find the actual word in original advice and replace
                        original_words = advice.split()
                        if i < len(original_words):
                            # Only replace if first letter matches (to avoid false positives)
                            if original_words[i][0].lower() == word[0].lower():
                                original_words[i] = (
                                    word.capitalize() if original_words[i][0].isupper() else word
                                )
                                advice = " ".join(original_words)

        # 3. Remove Cast suggestions for cards that cost more mana than available or missing required colors
        from arenamcp.rules_engine import RulesEngine

        current_mana_pool = RulesEngine._get_mana_pool(game_state, local_seat)
        potential_mana_pool = dict(current_mana_pool)
        potential_sources = list(current_mana_pool.get("_sources", []))

        # Add potential land drop from hand to the potential mana pool
        if lands_in_hand:
            hand_land_colors: set[str] = set()
            for c in hand_cards:
                if "land" in c.get("type_line", "").lower():
                    l_name = (c.get("name") or "").lower()
                    l_type = (c.get("type_line") or "").lower()
                    l_oracle = (c.get("oracle_text") or "").lower()

                    if "any color" in l_oracle or "mana of any type" in l_oracle:
                        hand_land_colors.update({"W", "U", "B", "R", "G"})
                    else:
                        for clr, bsc in [
                            ("W", "plains"),
                            ("U", "island"),
                            ("B", "swamp"),
                            ("R", "mountain"),
                            ("G", "forest"),
                        ]:
                            if bsc in l_name or bsc in l_type or f"{{{clr.lower()}}}" in l_oracle:
                                hand_land_colors.add(clr)
                        if "{c}" in l_oracle:
                            hand_land_colors.add("C")

            if not hand_land_colors:
                hand_land_colors = set("WUBRG")

            potential_mana_pool["total"] += 1
            potential_sources.append(frozenset(hand_land_colors))
            for clr in hand_land_colors:
                potential_mana_pool[clr] = potential_mana_pool.get(clr, 0) + 1

        potential_mana_pool["_sources"] = potential_sources
        potential_mana = potential_mana_pool["total"]

        # Check each card in hand for mana cost / color violations
        seen_card_names = set()
        for card in hand_cards:
            type_line = card.get("type_line", "").lower()
            if "land" in type_line:
                continue

            card_name = card.get("name", "")
            mana_cost = card.get("mana_cost", "")
            grp_id = card.get("grp_id")
            if card_name in seen_card_names or not card_name:
                continue
            seen_card_names.add(card_name)

            # If mana_cost is missing from hand card dict, try looking up in RulesEngine database
            if not mana_cost and grp_id:
                try:
                    card_db_entry = RulesEngine._card_db.get(grp_id) or {}
                    mana_cost = card_db_entry.get("mana_cost", "")
                except Exception:
                    pass

            cmc = RulesEngine._parse_cmc(mana_cost) if mana_cost else 0
            can_afford = (
                RulesEngine._can_afford(mana_cost, potential_mana_pool)
                if mana_cost
                else (cmc <= potential_mana)
            )

            # If this card costs more than we can have OR lacks required colors, remove Cast/Play suggestions for it
            if not can_afford:
                # Remove "then [cast|play] Card" sequences (e.g. "Play land then play Bothersome Noisemaker")
                then_pattern = re.compile(
                    rf",?\s*(?:then|and)\s+(?:cast|play)\s+{re.escape(card_name)}[.,]?\s*",
                    re.IGNORECASE,
                )
                if then_pattern.search(advice):
                    advice = then_pattern.sub(". ", advice).strip()
                    logger.debug(
                        f"Removed uncastable 'then' sequence: {card_name} (needs {mana_cost}, available total {potential_mana})"
                    )
                    continue

                # Remove "Cast/Play [Card Name]" as a standalone command (e.g. "Cast X." or "Play X.")
                standalone_pattern = re.compile(
                    rf"(?:^|(?<=\.\s)|(?<=\n))(?:Cast|Play)\s+{re.escape(card_name)}[.,]?\s*",
                    re.IGNORECASE,
                )
                if standalone_pattern.search(advice):
                    advice = standalone_pattern.sub("", advice)
                    logger.debug(
                        f"Removed uncastable suggestion: {card_name} (needs {mana_cost}, available total {potential_mana})"
                    )
                else:
                    # Card mentioned mid-sentence — replace name with "[uncastable]" hint
                    mid_pattern = re.compile(rf"(?:cast|play)\s+{re.escape(card_name)}", re.IGNORECASE)
                    if mid_pattern.search(advice):
                        advice = mid_pattern.sub(f"{card_name} (not enough mana)", advice, count=1)
                        logger.debug(
                            f"Annotated uncastable mid-sentence: {card_name} (needs {mana_cost}, available total {potential_mana})"
                        )

        # 4. Remove incorrect lethal/win claims when math doesn't support it
        if re.search(r"(?i)\blethal\b|\bfor the win\b|\bthat'?s the win\b|\bwin!\b", advice):
            summary = _combat_attack_summary()
            if summary:
                attack_power, opp_life, opp_blockers = summary
                if opp_blockers > 0 or attack_power < opp_life:
                    advice = re.sub(r"(?i)\blethal\b", "damage", advice)
                    advice = re.sub(r"(?i)\bfor the win\b", "for damage", advice)
                    advice = re.sub(r"(?i)\bthat'?s the win\b", "", advice)
                    advice = re.sub(r"(?i)\bwin!\b", "", advice)
                    advice = advice.replace("lethal on board", "pressure on board")

        # 5. Suicide / Self-Damage Safety Filter:
        # Prevent advising spells or lands that inflict self-damage when life total is too low.
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        your_life = local_player.get("life_total", 20) if local_player else 20

        if isinstance(your_life, int) and your_life <= 5:
            advice_lower = advice.lower()
            if "sunspine lynx" in advice_lower:
                nonbasics = sum(
                    1
                    for c in battlefield
                    if c.get("owner_seat_id") == local_seat
                    and "land" in c.get("type_line", "").lower()
                    and "basic" not in c.get("type_line", "").lower()
                )
                if your_life <= max(1, nonbasics):
                    advice = f"Pass priority. Do not cast Sunspine Lynx — its ETB nonbasic damage would deal {nonbasics} to you and be fatal at {your_life} life!"
            elif "thoughtseize" in advice_lower and your_life <= 2:
                advice = f"Pass priority. Do not cast Thoughtseize — the 2 life loss would be fatal at {your_life} life!"
            elif (
                any(
                    p in advice_lower
                    for p in [
                        "city of brass",
                        "mana confluence",
                        "battlefield forge",
                        "shivan reef",
                        "llanowar wastes",
                        "caves of koilos",
                        "adarkar wastes",
                        "karplusan forest",
                        "sulfurous springs",
                        "brushland",
                        "underground river",
                        "yavimaya coast",
                    ]
                )
                and your_life <= 1
            ):
                advice = f"Pass priority. Do not tap self-damage lands — taking 1 damage is fatal at {your_life} life!"

        # Equip / Attach target validator:
        # Prevent advising to equip an Equipment to a card in hand or off-battlefield.
        equip_match = re.search(
            r"(?i)\b(?:equip|attach)\s+([\w\s'—]+?)\s+(?:to|onto|on|with)\s+([\w\s'—]+)",
            advice,
        )
        if equip_match:
            g1, g2 = equip_match.group(1).strip(), equip_match.group(2).strip()
            if " with " in equip_match.group(0).lower():
                equip_name, target_name = g2, g1
            else:
                equip_name, target_name = g1, g2

            target_name = re.sub(r"[.,;!?]+$", "", target_name).strip()

            local_seat = None
            for p in game_state.get("players", []):
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
                    break

            hand_card_names = {
                (c.get("name") or "").lower() for c in game_state.get("hand", []) if c.get("name")
            }
            bf_creatures = [
                c.get("name")
                for c in game_state.get("battlefield", [])
                if (c.get("owner_seat_id") == local_seat or c.get("controller_seat_id") == local_seat)
                and "creature" in (c.get("type_line") or "").lower()
                and c.get("name")
            ]

            target_lower = target_name.lower()
            target_in_hand = any(
                target_lower in hn or hn in target_lower
                for hn in hand_card_names
                if len(target_lower) >= 3
            )
            target_on_bf = any(
                target_lower in bn.lower() or bn.lower() in target_lower
                for bn in bf_creatures
                if len(target_lower) >= 3
            )

            if target_in_hand and not target_on_bf:
                if bf_creatures:
                    valid_target = bf_creatures[0]
                    advice = re.sub(
                        re.escape(target_name), valid_target, advice, flags=re.IGNORECASE
                    )
                    logger.info(
                        f"Fixed invalid equip target '{target_name}' (card in hand) -> '{valid_target}' (on battlefield)"
                    )
                else:
                    advice = re.sub(
                        r"(?i)\b(?:equip|attach)\s+[\w\s'—]+?\s+(?:to|onto|on|with)\s+[\w\s'—]+[.,]?",
                        "",
                        advice,
                    ).strip()
                    logger.info(
                        f"Removed impossible equip target '{target_name}' (card in hand, no creatures on battlefield)"
                    )

        # Clean up double spaces
        advice = re.sub(r"\s+", " ", advice).strip()

        def _augment_legal_actions_from_decision_context(
            actions: list[str],
        ) -> list[str]:
            """Add high-signal combat actions from decision context when missing.

            RulesEngine legal actions can lag behind GRE decision context during
            declare-attack/block windows. In those states, prefer the concrete
            attacker/blocker sets from decision_context over generic activate/cast
            options so fallback advice remains action-appropriate.
            """
            augmented = list(actions)
            decision_context = game_state.get("decision_context") or {}
            dec_type = str(decision_context.get("type", "") or "").lower()

            if dec_type == "declare_attackers":
                legal_attackers = self._filter_legal_attacker_names(
                    game_state, decision_context.get("legal_attackers") or []
                )
                if legal_attackers:
                    attack_action = f"Declare Attackers: {', '.join(legal_attackers)}"
                    if all(a.lower() != attack_action.lower() for a in augmented):
                        augmented.append(attack_action)

            if dec_type == "declare_blockers":
                legal_blockers = decision_context.get("legal_blockers") or []
                if legal_blockers:
                    block_action = f"Block with: {', '.join(legal_blockers)}"
                    if all(a.lower() != block_action.lower() for a in augmented):
                        augmented.append(block_action)

            return augmented

        # 5. Enforce Legal actions only (hard filter)
        # MULLIGAN OVERRIDE: During mulligan, RulesEngine returns "Wait (Opponent
        # has priority)" because priority_player != local_seat. Override here just
        # like _format_game_context does (line ~1384).
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            legal_actions = ["KEEP", "MULLIGAN"]
        elif pending == "Mulligan Bottom":
            # During bottom-card selection, any card name advice is valid
            legal_actions = []
        else:
            try:
                from arenamcp.rules_engine import RulesEngine

                legal_actions = RulesEngine.get_legal_actions(game_state) or []
                legal_actions = _augment_legal_actions_from_decision_context(legal_actions)
            except Exception as e:
                logger.warning(f"RulesEngine error in postprocess: {e}")
                legal_actions = []

        if legal_actions:

            def _score_action(action: str) -> int:
                """Heuristic score for legal actions (higher is better)."""
                score = 0
                act = action.lower()
                turn = game_state.get("turn", {})
                phase = turn.get("phase", "").lower()
                step = turn.get("step", "").lower()
                pending_decision = str(game_state.get("pending_decision", "") or "").lower()
                players = game_state.get("players", [])
                local_player = next((p for p in players if p.get("is_local")), None)
                local_seat = local_player.get("seat_id") if local_player else None

                # Prefer land drop if available
                if act.startswith("play land:"):
                    score += 80

                # Combat step priorities
                if "declare attackers" in act and "combat" in phase and "declareattack" in step:
                    score += 90
                if "declare attackers" in act and "declare attackers" in pending_decision:
                    score += 120
                if "block with" in act and "combat" in phase and "declareblock" in step:
                    score += 120
                if "block with" in act and "declare blockers" in pending_decision:
                    score += 120

                # Strongly prefer actions confirmed castable by the game engine
                if "[ok]" in act:
                    score += 50

                # Casting is generally higher priority than activating
                if act.startswith("cast "):
                    if "[ok]" in act:
                        score += 60  # confirmed castable
                    else:
                        score += 10  # may not have mana — low priority
                if act.startswith("activate "):
                    score += 40
                if act.startswith("activate ") and (
                    ("combat" in phase and "declareblock" in step) or ("declare blockers" in pending_decision)
                ):
                    # During blocker declaration, avoid replacing with activations.
                    score -= 100

                # During combat, "Pass" (the Next button) is usually correct
                # when no cast/play/declare actions are available
                if act == "pass" and "combat" in phase:
                    score += 10

                # Target Selection scoring: prefer opponent targets for harmful spells/abilities, player targets for beneficial spells
                if act.startswith("select target:"):
                    is_yours = "(yours)" in act or "you" in act
                    is_opp = "(opp)" in act or "opponent" in act

                    decision_ctx = game_state.get("decision_context") or {}
                    source_oracle = str(
                        decision_ctx.get("source_oracle_text")
                        or decision_ctx.get("source_card_oracle_text")
                        or ""
                    ).lower()
                    if not source_oracle and game_state.get("stack"):
                        source_id = decision_ctx.get("source_id")
                        for obj in game_state.get("stack", []):
                            if obj.get("instance_id") == source_id:
                                source_oracle = (obj.get("oracle_text") or "").lower()
                                break
                    if not source_oracle:
                        source_card = str(decision_ctx.get("source_card") or "")
                        if source_card:
                            from arenamcp.rules_engine import RulesEngine
                            for card_info in RulesEngine._card_db.values():
                                if isinstance(card_info, dict) and (card_info.get("name") or "").lower() == source_card.lower():
                                    source_oracle = (card_info.get("oracle_text") or "").lower()
                                    if source_oracle:
                                        break

                    harmful_keywords = (
                        "deal",
                        "damage",
                        "destroy",
                        "exile",
                        "loses",
                        "fight",
                        "sacrifice",
                        "-1/",
                        "-2/",
                        "-3/",
                        "-4/",
                        "-5/",
                    )
                    beneficial_keywords = (
                        "+1/+1",
                        "draw a card",
                        "draws",
                        "gain life",
                        "equip",
                        "gains indestructible",
                        "hexproof",
                        "protection",
                        "gets +",
                    )
                    is_harmful = any(kw in source_oracle for kw in harmful_keywords)
                    is_beneficial = any(kw in source_oracle for kw in beneficial_keywords)

                    if is_harmful and not is_beneficial:
                        if is_opp:
                            score += 150
                        elif is_yours:
                            score -= 200
                    elif is_beneficial and not is_harmful:
                        if is_yours:
                            score += 150
                        elif is_opp:
                            score -= 200
                    else:
                        if is_opp:
                            score += 100
                        elif is_yours:
                            score -= 100

                return score

            def _normalize_best_legal_action(action: str) -> str:
                """Normalize fallback combat actions against visible legality."""
                act_lower = action.lower()

                if act_lower.startswith("declare attackers:"):
                    names = [n.strip() for n in action.split(":", 1)[1].split(",") if n.strip()]
                    filtered_names = self._filter_legal_attacker_names(game_state, names)
                    if not filtered_names:
                        return "Don't attack"
                    return f"Declare Attackers: {', '.join(filtered_names)}"

                if act_lower.startswith("block with:"):
                    names = [n.strip() for n in action.split(":", 1)[1].split(",") if n.strip()]
                    if not names:
                        return "Don't block"

                return action

            def _get_legal_pass_action(actions: list[str]) -> str | None:
                """Return the concrete legal Pass action when available."""
                for action in actions:
                    if action.strip().lower() == "pass":
                        return action
                return None

            def _has_pass_intent(text: str) -> bool:
                """Detect advice that means "do nothing now and let play proceed"."""
                lead_clause = re.split(r"(?<=[.!?;])\s+", text.strip(), maxsplit=1)[0].lower()
                pass_intent_patterns = (
                    r"\blet (?:it|that|this|them) resolve\b",
                    r"\bpass priority\b",
                    r"^\s*pass\b",
                    r"^\s*wait\b",
                    r"\bno response\b",
                    r"\bdon['’]?t respond\b",
                    r"\bdo not respond\b",
                    r"\blet them have it\b",
                    r"\bnothing to do\b",
                )
                return any(re.search(pattern, lead_clause) for pattern in pass_intent_patterns)

            advice_lower = advice.lower()
            legal_lower = [a.lower() for a in legal_actions]
            # Strip [OK], [NEED:x], [NO TARGETS] etc. markers before matching so
            # "Cast Northern Air Temple" matches "Cast Northern Air Temple [NEED:B]"
            legal_lower_stripped = [re.sub(r"\s*\[[^\]]+\]", "", a).strip() for a in legal_lower]
            matches = any(l in advice_lower for l in legal_lower) or any(
                l in advice_lower for l in legal_lower_stripped
            )
            legal_pass_action = _get_legal_pass_action(legal_actions)

            if not matches and legal_pass_action and _has_pass_intent(advice):
                advice = legal_pass_action
                advice_lower = advice.lower()
                matches = True

            # "Don't attack", "don't block", "pass priority", "no attacks" are
            # always valid strategic choices — the player can decline to act.
            PASSTHROUGH_PHRASES = [
                "don't attack",
                "don't attack",
                "do not attack",
                "no attack",
                "don't block",
                "don't block",
                "do not block",
                "no block",
                "pass priority",
                "take the damage",
                "let it resolve",
                "let them resolve",
                "let that resolve",
                "wait",
                "no response",
                "don't respond",
                "don't respond",
                "nothing to do",
                "pass",
                "resolve",
                "select",
                "choose",
                "pick",
                "both options",
                "option 1",
                "option 2",
                "first option",
                "second option",
            ]
            dec_ctx = game_state.get("decision_context") or {}
            dec_type = str(dec_ctx.get("type", "") or "").lower()
            if dec_type in (
                "select_items",
                "select_targets",
                "target_selection",
                "modal_choice",
                "distribution",
                "pay_costs",
                "numeric_input",
            ):
                if any(
                    word in advice_lower
                    for word in [
                        "select",
                        "choose",
                        "pick",
                        "option",
                        "both",
                        "mode",
                        "target",
                        "accept",
                        "decline",
                    ]
                ):
                    matches = True
            has_ok_actions = any(
                "[ok]" in act.lower() for act in legal_actions if not act.lower().startswith("pass")
            )
            false_no_mana_claim = any(
                claim in advice_lower
                for claim in [
                    "lack the mana",
                    "lacks the mana",
                    "don't have the mana",
                    "dont have the mana",
                    "not enough mana",
                    "no castable spells",
                    "cannot cast any",
                    "can't cast any",
                    "no legal spells",
                    "no playable spells",
                ]
            )

            if not matches and any(p in advice_lower for p in PASSTHROUGH_PHRASES):
                if not (has_ok_actions and false_no_mana_claim):
                    matches = True

            # Enhanced advice matching for partial card names, generic attacks/blocks, activations
            if not matches:
                for act in legal_actions:
                    act_lower = act.lower()
                    act_clean = re.sub(r"\s*\[[^\]]+\]", "", act_lower).strip()

                    # 1. Cast actions (e.g., "cast michelangelo, weirdness to 11")
                    if act_clean.startswith("cast "):
                        card_name = act_clean[5:].strip()
                        short_name = re.split(r"[,—/]", card_name)[0].strip()
                        if short_name and short_name in advice_lower:
                            # Do not count as a cast match if short_name is only the target of an equip/attach action
                            is_equip_target = bool(
                                re.search(
                                    rf"(?i)\b(?:equip|attach)\b.*\b{re.escape(short_name)}\b",
                                    advice_lower,
                                )
                            ) and not bool(
                                re.search(
                                    rf"(?i)\b(?:cast|play)\b.*\b{re.escape(short_name)}\b",
                                    advice_lower,
                                )
                            )
                            if not is_equip_target:
                                matches = True
                                break

                    # 2. Play land actions (e.g., "play land: forest")
                    elif act_clean.startswith("play land:"):
                        card_name = act_clean[10:].strip()
                        short_name = re.split(r"[,—/]", card_name)[0].strip()
                        if short_name and (
                            short_name in advice_lower
                            or "play land" in advice_lower
                            or "play a land" in advice_lower
                        ):
                            matches = True
                            break

                    # 3. Activate actions (e.g., "activate bristly bill, spine sower" or "activate ability: well-worn spatula")
                    elif act_clean.startswith("activate "):
                        card_name = re.sub(r"(?i)^ability:\s*", "", act_clean[9:]).strip()
                        short_name = re.split(r"[,—/]", card_name)[0].strip()
                        card_words = [
                            w
                            for w in re.split(r"[\s\-_,]", card_name)
                            if len(w) >= 4 and w.lower() not in ("ability", "card")
                        ]
                        if short_name and (
                            short_name in advice_lower
                            or any(w in advice_lower for w in card_words)
                        ):
                            matches = True
                            break

                    # 4. Declare attackers (e.g., "declare attackers: bristly bill, spine sower...")
                    elif act_clean.startswith("declare attackers:"):
                        names_str = act_clean.split(":", 1)[1]
                        names = [n.strip() for n in re.split(r"[,#\d]", names_str) if n.strip()]
                        name_matched = any(name in advice_lower for name in names if len(name) > 2)

                        is_negative = any(
                            neg in advice_lower
                            for neg in [
                                "don't",
                                "dont",
                                "do not",
                                "no attack",
                                "not attack",
                                "hold back",
                                "never attack",
                                "avoid attacking",
                                "decline to attack",
                            ]
                        )
                        generic_attack = any(
                            phrase in advice_lower
                            for phrase in [
                                "attack with all",
                                "attack with everything",
                                "all attack",
                                "swing with all",
                                "swing with everything",
                                "attack all",
                                "swing all",
                                "attack with everyone",
                                "swing with everyone",
                                "all in",
                                "all-in",
                                "attack with all creatures",
                                "swing with all creatures",
                                "all creatures attack",
                                "attack with your creatures",
                                "swing with your creatures",
                                "attack with all of your",
                                "swing with all of your",
                                "attack with all available",
                                "swing with all available",
                                "attack with your team",
                                "swing with your team",
                                "attack with the team",
                                "swing with the team",
                                "attack!",
                                "attack.",
                                "swing!",
                                "swing.",
                            ]
                        ) or advice_lower.strip() in ("attack", "swing")

                        if (name_matched or generic_attack) and not is_negative:
                            matches = True
                            break

                    # 5. Block actions (e.g., "block with: ...")
                    elif act_clean.startswith("block with:"):
                        is_negative = any(
                            neg in advice_lower
                            for neg in [
                                "don't",
                                "dont",
                                "do not",
                                "no block",
                                "not block",
                                "never block",
                                "avoid blocking",
                                "decline to block",
                                "no blocks",
                            ]
                        )
                        generic_block = any(
                            phrase in advice_lower
                            for phrase in [
                                "block with all",
                                "block with everything",
                                "all block",
                                "block all",
                                "block with everyone",
                                "block with all creatures",
                                "block with your creatures",
                                "block with all available",
                                "block with your team",
                                "block with the team",
                                "block!",
                                "block.",
                            ]
                        ) or advice_lower.strip() in ("block", "blocking")

                        if generic_block and not is_negative:
                            matches = True
                            break

                    # 6. Done (confirm attackers) - matches if LLM recommends attacking
                    elif act_clean == "done (confirm attackers)":
                        is_negative = any(
                            neg in advice_lower
                            for neg in [
                                "don't",
                                "dont",
                                "do not",
                                "no attack",
                                "not attack",
                                "hold back",
                                "never attack",
                                "avoid attacking",
                                "decline to attack",
                            ]
                        )
                        has_attack_intent = any(
                            phrase in advice_lower
                            for phrase in ["attack", "swing", "lethal", "all in", "all-in", "combat"]
                        )
                        if has_attack_intent and not is_negative:
                            matches = True
                            break

                    # 7. Done (confirm blockers) - matches if LLM recommends blocking
                    elif act_clean == "done (confirm blockers)":
                        is_negative = any(
                            neg in advice_lower
                            for neg in [
                                "don't",
                                "dont",
                                "do not",
                                "no block",
                                "not block",
                                "never block",
                                "avoid blocking",
                                "decline to block",
                                "no blocks",
                            ]
                        )
                        has_block_intent = any(
                            phrase in advice_lower for phrase in ["block", "chump", "trade"]
                        )
                        if has_block_intent and not is_negative:
                            matches = True
                            break

            if not matches and is_backend_error_text(advice):
                # Transport/auth failure — NEVER mask it as coaching. On
                # 2026-07-16 an empty license key produced 435 silent 401s
                # while the fallback scorer replaced every response with a
                # plausible legal action; the user debugged "bad advice"
                # for hours when the truth was "the LLM never spoke once".
                low = advice.lower()
                if "401" in advice or "403" in advice or "virtual key" in low or "auth" in low:
                    advice = (
                        "No coaching available: the gateway rejected your "
                        "license key. Open the Repair tab and run Fix "
                        "Everything."
                    )
                else:
                    advice = "No coaching available: cannot reach the AI service right now."
                logger.error(f"LLM failure surfaced to user (not masked): {advice}")
            elif not matches:
                # Force to best legal action to avoid illegal recommendations
                turn = game_state.get("turn", {})
                phase = str(turn.get("phase", "") or "").lower()
                step = str(turn.get("step", "") or "").lower()
                pending_decision = str(game_state.get("pending_decision", "") or "").lower()

                # Filter out [NO TARGETS] cards — casting them wastes the card.
                # Recompute from game state: spells needing "target creature you
                # control" when we have no creatures (Sagas exempt).
                _no_target_names: set[str] = set()
                _hand = game_state.get("hand", [])
                _bf = game_state.get("battlefield", [])
                _lp = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
                _ls = _lp.get("seat_id") if _lp else None
                _my_creatures = [
                    c
                    for c in _bf
                    if c.get("owner_seat_id") == _ls
                    and c.get("power") is not None
                    and "land" not in c.get("type_line", "").lower()
                ]
                if not _my_creatures:
                    for _hc in _hand:
                        _oracle = (_hc.get("oracle_text") or "").lower()
                        _tl = (_hc.get("type_line") or "").lower()
                        if "land" not in _tl and "creature" not in _tl and "saga" not in _tl:
                            if (
                                "target creature you control" in _oracle
                                or "creature you control fights" in _oracle
                            ):
                                _hname = _hc.get("name")
                                if _hname:
                                    _no_target_names.add(_hname)

                # Build candidate pool excluding [NO TARGETS] cards
                if _no_target_names:
                    _candidates = [
                        a
                        for a in legal_actions
                        if not any(f"Cast {nt}".lower() in a.lower() for nt in _no_target_names)
                    ]
                else:
                    _candidates = legal_actions
                if not _candidates:
                    _candidates = legal_actions  # fallback to unfiltered

                in_declare_blockers = ("combat" in phase and "declareblock" in step) or (
                    "declare blockers" in pending_decision
                )
                if in_declare_blockers:
                    blocker_actions = [a for a in _candidates if a.lower().startswith("block with:")]
                    if blocker_actions:
                        best = max(blocker_actions, key=_score_action)
                    else:
                        best = max(_candidates, key=_score_action)
                else:
                    if legal_pass_action and ("need:" in advice.lower() or "[need:" in advice.lower()):
                        best = legal_pass_action
                    else:
                        best = max(_candidates, key=_score_action)
                best = _normalize_best_legal_action(best)
                logger.info(f"Replaced illegal advice with legal action: {best} (original: {advice[:80]})")
                advice = best
                # This line came from the deterministic scorer, not the LLM.
                # Untagged it is indistinguishable from model advice — the
                # exact failure mode the health tags exist to prevent.
                local_fallback_used = True
        else:
            # No legal_actions reported. For passable idle windows this
            # means "pass priority" — but for SelectTargets/Search/Modal/
            # PayCosts the LLM's targeted answer is the best signal we
            # have (RulesEngine can't enumerate candidates for these).
            # Keep the model's advice unless it's clearly useless; only
            # then fall back to the context-appropriate manual prompt.
            req_class = str(game_state.get("_bridge_request_class") or "")
            req_type = str(game_state.get("_bridge_request_type") or "")
            non_passable = (
                req_class in _NON_PASSABLE_REQUEST_CLASSES
                or req_type in _NON_PASSABLE_REQUEST_TYPES
                or game_state.get("_bridge_can_pass") is False
            )
            stripped = (advice or "").strip()
            looks_useful = (
                bool(stripped)
                and len(stripped) >= 3
                and "pass priority" not in stripped.lower()
                and "pass" not in stripped.lower().split()[:1]
            )
            if non_passable and looks_useful:
                # Trust the LLM's targeted advice on non-passable requests.
                pass
            else:
                advice = _fallback_non_action_advice(game_state)

        # Clean up internal action format for spoken output:
        # "Play Land: Plains" → "Play Plains"
        advice = re.sub(r"(?i)^Play Land:\s*", "Play ", advice)
        advice = re.sub(r"(?i)Play Land:\s*", "Play ", advice)
        if str(game_state.get("pending_decision", "") or "").lower() == "declare attackers":
            advice = re.sub(r"(?i)^Done \(confirm attackers\)$", "Don't attack", advice)
        if str(game_state.get("pending_decision", "") or "").lower() == "declare blockers":
            advice = re.sub(r"(?i)^Done \(confirm blockers\)$", "Don't block", advice)

        # Sequence & Mana Budget validator:
        # If advice recommends casting multiple non-land spells whose total CMC exceeds
        # total available mana (current or post-land), strip the extra spells.
        if isinstance(game_state, dict):
            from arenamcp.rules_engine import RulesEngine

            lp = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
            ls = lp.get("seat_id") if lp else 1
            cur_mana = RulesEngine._count_available_mana(game_state, ls)

            hand = game_state.get("hand", [])
            land_in_advice = bool(
                re.search(
                    r"(?i)\bplay\s+([\w\s'—]+?\b(?:forest|plains|island|swamp|mountain|land))\b",
                    advice,
                )
            )
            avail_mana = cur_mana + 1 if land_in_advice else cur_mana

            advice_low = advice.lower()
            mentioned_spells = []
            for c in hand:
                if "land" in c.get("type_line", "").lower():
                    continue
                name = c.get("name", "")
                if not name:
                    continue
                short_name = re.split(r"[,—/]", name)[0].strip().lower()
                if len(short_name) >= 3 and short_name in advice_low:
                    mentioned_spells.append(c)

            if len(mentioned_spells) > 1:
                total_cmc = sum(
                    RulesEngine._parse_cmc(c.get("mana_cost", "")) for c in mentioned_spells
                )
                if total_cmc > avail_mana:
                    first_spell_name = mentioned_spells[0].get("name", "")
                    land_match = re.search(
                        r"(?i)^(Play\s+[\w\s'—]+?)(?:\s+(?:then|and)\s+cast\b.*)?$",
                        advice.strip(),
                    )
                    if land_in_advice and land_match:
                        land_part = land_match.group(1).strip()
                        advice = f"{land_part} then cast {first_spell_name}."
                    else:
                        advice = f"Cast {first_spell_name}."
                    logger.info(
                        f"Stripped impossible multi-spell advice (total CMC {total_cmc} > {avail_mana} mana) -> '{advice}'"
                    )

        # Sequence validator: If advice says "Play [land] then cast/play [spell]" or "Play [land] and cast/play [spell]"
        # but [spell] is illegal and not in post-land THEN options, strip the illegal spell clause.
        if any(kw in advice.lower() for kw in (" then cast ", " and cast ", " then play ", " and play ")):
            match_seq = re.search(
                r"(?i)^(Play\s+[\w\s'—]+?)(?:\s+(?:then|and)\s+(?:cast|play)\s+(.+))$", advice.strip()
            )
            if match_seq:
                land_part = match_seq.group(1).strip()
                spell_part = match_seq.group(2).strip()
                spell_part_clean = re.sub(r"(?i)\s+(?:to|for|and)\s+.*$", "", spell_part).strip()
                spell_short = re.split(r"[,—/]", spell_part_clean)[0].strip().lower()

                ok_cast_actions_exist = any(
                    "[ok]" in act.lower() for act in legal_actions if act.lower().startswith("cast ")
                )
                spell_is_legal = any(
                    spell_short in act.lower() and (not ok_cast_actions_exist or "[ok]" in act.lower())
                    for act in legal_actions
                    if act.lower().startswith("cast ")
                )
                then_lines = [l for l in legal_actions if l.startswith("THEN:")]
                if not then_lines and isinstance(game_state, dict):
                    prompt_lines = game_state.get("_last_prompt_lines", [])
                    then_lines = [l for l in prompt_lines if l.startswith("THEN:")]

                spell_in_then = any(spell_short in tl.lower() for tl in then_lines)

                # Check if the spell is castable after the land drop using potential_mana_pool
                can_cast_post_land = False
                spell_card = next(
                    (c for c in hand_cards if spell_short in (c.get("name") or "").lower()),
                    None,
                )
                if spell_card:
                    m_cost = spell_card.get("mana_cost", "")
                    if not m_cost and spell_card.get("grp_id"):
                        with contextlib.suppress(Exception):
                            m_cost = RulesEngine._card_db.get(spell_card.get("grp_id"), {}).get("mana_cost", "")
                    can_cast_post_land = RulesEngine._can_afford(m_cost, potential_mana_pool) if m_cost else True

                if not spell_is_legal and not spell_in_then and not can_cast_post_land:
                    logger.info(
                        f"Stripped illegal post-land spell '{spell_part}' from advice '{advice}' -> '{land_part}'"
                    )
                    advice = land_part

        # Summoning Sickness validator:
        # If advice recommends "Cast [creature] then attack" or "Play [creature] and attack",
        # check if the creature lacks haste. If it lacks haste, strip the attack clause.
        if isinstance(game_state, dict):
            match_atk = re.search(
                r"(?i)\b(?:cast|play)\s+(.+?)\s+(?:then|and)\s+attack\b", advice.strip()
            )
            if match_atk:
                c_name = match_atk.group(1).strip()
                c_name = re.sub(r"\s*\(.*?\)", "", c_name).strip()
                hand = game_state.get("hand", [])
                bf = game_state.get("battlefield", [])
                card_obj = next(
                    (c for c in hand + bf if c_name.lower() in (c.get("name") or "").lower()), None
                )
                if card_obj and "creature" in (card_obj.get("type_line") or "").lower():
                    oracle = (card_obj.get("oracle_text") or "").lower()
                    if "haste" not in oracle:
                        advice = re.sub(
                            r"(?i)\s+(?:then|and)\s+attack\b.*$", "", advice.strip()
                        ).strip()
                        if not advice.endswith("."):
                            advice += "."
                        logger.info(
                            f"Stripped invalid attack clause for summoning-sick creature -> '{advice}'"
                        )

        # 6. Block advice must name the attacker. "Block with Veteran Survivor"
        # is useless with multiple attackers on board (issue #420) — repair it
        # with the deterministic solver's assignment so the spoken line is
        # always actionable.
        advice = self._ensure_block_advice_names_attacker(advice, game_state)

        # 7. Health-tag normalization — MUST be last. Tags locally generated
        # advice, and hoists any tag that a caller's prepended framing (the
        # game-plan intro) buried mid-string, where the startswith-based
        # strip_health_tags used at the TTS boundary cannot remove it and
        # the voice reads "local fallback" out loud.
        advice = _normalize_health_tags(advice, force_local_fallback=local_fallback_used)

        return advice

    def _collect_block_decision_blockers(self, game_state: dict[str, Any]) -> list[dict]:
        """Resolve the legal blockers for the current block decision.

        Prefers the GRE-authoritative ``legal_blocker_ids`` from the
        decision context; falls back to our untapped creatures.
        """
        battlefield = game_state.get("battlefield", []) or []
        ctx = game_state.get("decision_context") or {}
        ids: set[int] = set()
        if str(ctx.get("type") or "") == "declare_blockers":
            for bid in ctx.get("legal_blocker_ids") or []:
                try:
                    ids.add(int(bid))
                except (TypeError, ValueError):
                    continue
        if ids:
            cards = [c for c in battlefield if int(c.get("instance_id") or 0) in ids]
            if cards:
                return cards
        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        return [
            c
            for c in battlefield
            if c.get("owner_seat_id") == local_seat
            and "creature" in (c.get("type_line") or "").lower()
            and not c.get("is_tapped")
            and not self._is_impending(c)
        ]

    @staticmethod
    def _spoken_name_map(cards: list[dict]) -> dict[int, str]:
        """instance_id -> plain spoken name, ``#N``-deduped for duplicates."""
        names = [c.get("name") or "?" for c in cards]
        counts = Counter(names)
        seen: dict[str, int] = {}
        out: dict[int, str] = {}
        for c, n in zip(cards, names, strict=False):
            label = n
            if counts[n] > 1:
                seen[n] = seen.get(n, 0) + 1
                label = f"{n} #{seen[n]}"
            out[int(c.get("instance_id") or 0)] = label
        return out

    def _solver_block_assignment_sentence(
        self,
        game_state: dict[str, Any],
        attackers: list[dict],
        blockers: list[dict],
        advice: str,
    ) -> str:
        """Deterministic "block A with X; block B with Y" sentence.

        Uses combat_solver.optimal_blocks (honoring the GRE per-blocker
        candidate restrictions when available). When the solver prefers no
        blocks but the advice insists on blocking, points the mentioned (or
        first) blocker at the biggest attacker it can legally block so the
        spoken line stays actionable.
        """
        if not attackers or not blockers:
            return ""
        try:
            from arenamcp.combat_solver import (
                blocker_allowed_attackers_map,
                optimal_blocks,
            )
        except Exception:
            return ""
        players = game_state.get("players", []) or []
        local_player = next((p for p in players if p.get("is_local")), None)
        your_life = local_player.get("life_total", 20) if local_player else 20
        ctx = game_state.get("decision_context") or {}
        allowed_map = blocker_allowed_attackers_map(ctx.get("raw_blockers") or [])

        atk_names = self._spoken_name_map(attackers)
        blk_names = self._spoken_name_map(blockers)

        # When the advice already names specific blockers, repair THOSE lines
        # (the LLM may have a reason the solver can't see — Calculator+Coach:
        # the solver supplies the missing attacker, it doesn't override the
        # pick). Fall back to the full blocker pool if that yields nothing.
        advice_lower = advice.lower()
        mentioned = [
            b
            for b in blockers
            if (b.get("name") or "").lower() and (b.get("name") or "").lower() in advice_lower
        ]
        candidate_pools: list[list[dict]] = []
        if mentioned:
            candidate_pools.append(mentioned)
        candidate_pools.append(blockers)

        assignments: dict[int, int] = {}
        for pool in candidate_pools:
            try:
                plan = optimal_blocks(
                    attackers,
                    pool,
                    your_life,
                    blocker_allowed_attackers=allowed_map or None,
                )
            except Exception as e:
                logger.debug(f"combat solver (block repair) failed: {e}")
                continue
            if plan is not None and plan.assignments:
                assignments = plan.assignments
                break

        clauses: list[str] = []
        if assignments:
            by_attacker: dict[int, list[int]] = {}
            for bid, aid in assignments.items():
                by_attacker.setdefault(int(aid), []).append(int(bid))
            for aid in sorted(by_attacker):
                a_label = atk_names.get(aid)
                b_labels = [blk_names.get(bid, f"creature {bid}") for bid in sorted(by_attacker[aid])]
                if a_label:
                    clauses.append(f"block {a_label} with {' and '.join(b_labels)}")
        else:
            # Solver says no blocks, but the advice recommends blocking —
            # keep the line actionable: aim the mentioned (or first) blocker
            # at the biggest attacker it can legally block.
            for b in mentioned or blockers[:1]:
                bid = int(b.get("instance_id") or 0)
                allowed = allowed_map.get(bid) if allowed_map else None
                candidates = []
                for a in attackers:
                    aid = int(a.get("instance_id") or 0)
                    if allowed is not None and aid not in allowed:
                        continue
                    if self._compute_combat_trade(a, b) is None:
                        continue  # can't legally block it (e.g. flying)
                    candidates.append(a)
                if not candidates:
                    continue
                biggest = max(candidates, key=lambda c: c.get("power") or 0)
                aid = int(biggest.get("instance_id") or 0)
                a_label = atk_names.get(aid)
                if a_label:
                    clauses.append(f"block {a_label} with {blk_names.get(bid, b.get('name', '?'))}")
        if not clauses:
            return ""
        return "Assignment: " + "; ".join(clauses) + "."

    def _ensure_block_advice_names_attacker(self, advice: str, game_state: dict[str, Any]) -> str:
        """Repair DeclareBlockers advice that names a blocker but no attacker.

        "Block with Veteran Survivor" is useless when multiple creatures are
        attacking (issue #420, first Mac match). If the advice recommends
        blocking but names no attacker from the current combat, append the
        deterministic solver's attacker->blocker assignment so the spoken
        line is always actionable. Negative advice ("don't block") and advice
        that already names an attacker pass through untouched.
        """
        if not advice:
            return advice
        pending = str(game_state.get("pending_decision") or "").lower()
        ctx = game_state.get("decision_context") or {}
        if pending != "declare blockers" and str(ctx.get("type") or "") != "declare_blockers":
            return advice

        import re

        advice_lower = advice.lower()
        if any(p in advice_lower for p in self._NEGATIVE_BLOCK_PHRASES):
            return advice

        attackers = self._collect_block_decision_attackers(game_state)
        if not attackers:
            return advice

        # Already names an attacker? Full-name match, or any distinctive
        # (len >= 4) word from an attacker's name appearing in the advice.
        advice_words = set(re.findall(r"[a-z'’]+", advice_lower))
        for atk in attackers:
            name = (atk.get("name") or "").lower()
            if not name:
                continue
            if name in advice_lower:
                return advice
            for word in re.findall(r"[a-z'’]+", name):
                if len(word) >= 4 and word in advice_words:
                    return advice

        # Only repair advice that is actually recommending a block.
        blockers = self._collect_block_decision_blockers(game_state)
        blocker_named = any((b.get("name") or "").lower() in advice_lower for b in blockers if b.get("name"))
        if "block" not in advice_lower and not blocker_named:
            return advice

        assignment = self._solver_block_assignment_sentence(game_state, attackers, blockers, advice)
        if not assignment:
            return advice
        base = advice.rstrip()
        if base and base[-1] not in ".!?":
            base += "."
        logger.info(
            "Block advice named no attacker; appended solver assignment: %s",
            assignment,
        )
        return f"{base} {assignment}"

    def _is_similar(self, a: str, b: str, threshold: float = 0.7) -> bool:
        """Check if two strings are similar using simple character overlap."""
        if a == b:
            return True
        if abs(len(a) - len(b)) > 3:
            return False
        # Count matching characters
        matches = sum(1 for c1, c2 in zip(a.lower(), b.lower(), strict=False) if c1 == c2)
        similarity = matches / max(len(a), len(b))
        return similarity >= threshold
