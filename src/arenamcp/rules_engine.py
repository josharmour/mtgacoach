
import logging
import re
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class RulesEngine:
    """
    A deterministic rules engine to calculate legal game actions.
    Serves as a 'Grounding' layer for the AI.
    """

    @staticmethod
    def _count_available_mana(game_state: Dict[str, Any], local_seat: int) -> int:
        """Count total available mana from untapped lands and mana creatures."""
        pool = RulesEngine._get_mana_pool(game_state, local_seat)
        return pool["total"]

    @staticmethod
    def _get_mana_pool(game_state: Dict[str, Any], local_seat: int) -> Dict[str, int]:
        """Get available mana pool with color breakdown from untapped sources."""
        battlefield = game_state.get("battlefield", [])
        pool = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 0, "total": 0}
        turn_num = game_state.get("turn", {}).get("turn_number", 0)
        creature_mana_source_count = 0
        your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat]
        for card in your_cards:
            if card.get("is_tapped"):
                continue
            type_line = card.get("type_line", "").lower()
            oracle = card.get("oracle_text", "")
            name = card.get("name", "")
            is_land = "land" in type_line
            is_creature = "creature" in type_line
            has_mana_ability = bool(re.search(r'\{T\}.*[Aa]dd\s+(\{|one |two |three )', oracle))
            # Detect lands with basic subtypes but no explicit "add" (e.g. Multiversal Passage)
            if is_land and not has_mana_ability:
                for basic in ("plains", "island", "swamp", "mountain", "forest"):
                    if basic in type_line:
                        has_mana_ability = True
                        break
            entered = card.get("turn_entered_battlefield", -1)
            has_haste = "haste" in oracle.lower()
            is_sick = is_creature and (entered == turn_num) and not has_haste
            if is_land or (is_creature and has_mana_ability and not is_sick):
                pool["total"] += 1
                if is_creature and has_mana_ability and not is_sick:
                    creature_mana_source_count += 1
                if "Plains" in name or "plains" in type_line or "{W}" in oracle:
                    pool["W"] += 1
                if "Island" in name or "island" in type_line or "{U}" in oracle:
                    pool["U"] += 1
                if "Swamp" in name or "swamp" in type_line or "{B}" in oracle:
                    pool["B"] += 1
                if "Mountain" in name or "mountain" in type_line or "{R}" in oracle:
                    pool["R"] += 1
                if "Forest" in name or "forest" in type_line or "{G}" in oracle:
                    pool["G"] += 1
                if "{C}" in oracle:
                    pool["C"] += 1
                if "any color" in oracle.lower():
                    pool["Any"] += 1

        # Detect bonus-mana effects: "whenever you tap a creature for mana, add"
        if creature_mana_source_count > 0:
            for card in your_cards:
                oracle_lower = card.get("oracle_text", "").lower()
                bonus_match = re.search(r"whenever you tap a creature for mana,?\s*add an additional \{(\w)\}", oracle_lower)
                if bonus_match:
                    bonus_color = bonus_match.group(1).upper()
                    pool["total"] += creature_mana_source_count
                    if bonus_color in pool:
                        pool[bonus_color] += creature_mana_source_count
        return pool

    @staticmethod
    def _can_afford(mana_cost: str, mana_pool: Dict[str, int]) -> bool:
        """Check if a spell can be cast with the available mana pool (total + colors)."""
        if not mana_cost:
            return True
        cmc = RulesEngine._parse_cmc(mana_cost)
        if mana_pool["total"] < cmc:
            return False
        # Check each color requirement
        for color in "WUBRGC":
            count = len(re.findall(rf"\{{{color}\}}", mana_cost))
            if count > 0:
                if mana_pool.get(color, 0) + mana_pool.get("Any", 0) < count:
                    return False
        return True

    @staticmethod
    def _parse_cmc(mana_cost: str) -> int:
        """Parse converted mana cost from a mana cost string like '{3}{R}{R}'."""
        if not mana_cost:
            return 0
        cmc = 0
        generic = re.findall(r'\{(\d+)\}', mana_cost)
        cmc += sum(int(g) for g in generic)
        for color in "WUBRGC":
            cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
        # Hybrid mana symbols like {U/R} count as 1 each
        hybrid = re.findall(r'\{[^}]+/[^}]+\}', mana_cost)
        cmc += len(hybrid)
        return cmc

    @staticmethod
    def _disambiguate_names(names: List[str]) -> List[str]:
        """Add #1, #2 suffixes to duplicate names in a list."""
        from collections import Counter
        counts = Counter(names)
        seen = {}
        result = []
        for name in names:
            if counts[name] > 1:
                seen[name] = seen.get(name, 0) + 1
                result.append(f"{name} #{seen[name]}")
            else:
                result.append(name)
        return result

    @staticmethod
    def _infer_target_requirements(oracle_text: str) -> Dict[str, Any]:
        """Infer rough target constraints from oracle text."""
        text = (oracle_text or "").lower()
        req = {
            "types": set(),
            "player_target": False,
            "planeswalker_target": False,
            "permanent_target": False,
            "nonland_only": False,
            "must_control": None,  # "you" | "opponent" | None
            "zones": set(),  # battlefield, stack, graveyard
            "target_spell": False,
            "target_ability": False,
            "must_be_attacking": False,
            "must_be_blocking": False,
            "must_be_tapped": False,
            "must_be_untapped": False,
            "must_have_flying": False,
            "power_ge": None,
            "power_le": None,
            "toughness_ge": None,
            "toughness_le": None,
            "mana_value_ge": None,
            "mana_value_le": None,
        }

        if "earthbend" in text:
            req["types"].add("land")
            req["permanent_target"] = True
            req["must_control"] = "you"
            req["zones"].add("battlefield")
            return req

        if "target opponent" in text:
            req["player_target"] = True
            req["must_control"] = "opponent"
            return req

        if "target player" in text:
            req["player_target"] = True

        if "target planeswalker" in text:
            req["planeswalker_target"] = True
            req["types"].add("planeswalker")

        if "target spell" in text:
            req["target_spell"] = True
            req["zones"].add("stack")
        if "target ability" in text or "target activated ability" in text or "target triggered ability" in text:
            req["target_ability"] = True
            req["zones"].add("stack")

        if "target creature spell" in text:
            req["target_spell"] = True
            req["types"].add("creature")
            req["zones"].add("stack")
        if "target instant or sorcery spell" in text:
            req["target_spell"] = True
            req["types"].update(["instant", "sorcery"])
            req["zones"].add("stack")

        if "target creature or planeswalker" in text:
            req["types"].update(["creature", "planeswalker"])
        elif "target creature" in text:
            req["types"].add("creature")

        if "target nonland permanent" in text:
            req["permanent_target"] = True
            req["nonland_only"] = True
        elif "target permanent" in text:
            req["permanent_target"] = True

        if "target artifact" in text:
            req["types"].add("artifact")
        if "target enchantment" in text:
            req["types"].add("enchantment")
        if "target land" in text:
            req["types"].add("land")

        if "graveyard" in text:
            req["zones"].add("graveyard")

        if "attacking" in text:
            req["must_be_attacking"] = True
        if "blocking" in text:
            req["must_be_blocking"] = True
        if "untapped" in text:
            req["must_be_untapped"] = True
        if "tapped" in text:
            req["must_be_tapped"] = True
        if "with flying" in text:
            req["must_have_flying"] = True

        for m in re.findall(r"power\s+(\d+)\s+or\s+greater", text):
            req["power_ge"] = int(m)
        for m in re.findall(r"power\s+(\d+)\s+or\s+less", text):
            req["power_le"] = int(m)
        for m in re.findall(r"toughness\s+(\d+)\s+or\s+greater", text):
            req["toughness_ge"] = int(m)
        for m in re.findall(r"toughness\s+(\d+)\s+or\s+less", text):
            req["toughness_le"] = int(m)
        for m in re.findall(r"mana value\s+(\d+)\s+or\s+greater", text):
            req["mana_value_ge"] = int(m)
        for m in re.findall(r"mana value\s+(\d+)\s+or\s+less", text):
            req["mana_value_le"] = int(m)

        if "you control" in text:
            req["must_control"] = "you"
        elif "opponent controls" in text or "an opponent controls" in text:
            req["must_control"] = "opponent"

        return req

    @staticmethod
    def _match_battlefield_targets(
        battlefield: List[Dict[str, Any]],
        local_seat: int,
        opponent_seat: int | None,
        req: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        for card in battlefield:
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if req["must_control"] == "you" and controller != local_seat:
                continue
            if req["must_control"] == "opponent" and opponent_seat is not None and controller != opponent_seat:
                continue

            type_line = (card.get("type_line") or "").lower()
            if req["nonland_only"] and "land" in type_line:
                continue

            if req["types"]:
                # Allow "permanent" without narrowing types
                type_match = any(t in type_line for t in req["types"])
                if not type_match:
                    continue

            if req["must_be_attacking"] and not card.get("is_attacking"):
                continue
            if req["must_be_blocking"] and not card.get("is_blocking"):
                continue
            if req["must_be_tapped"] and not card.get("is_tapped"):
                continue
            if req["must_be_untapped"] and card.get("is_tapped"):
                continue
            if req["must_have_flying"] and "flying" not in (card.get("oracle_text") or "").lower():
                continue

            power = card.get("power")
            toughness = card.get("toughness")
            if req["power_ge"] is not None and (power is None or power < req["power_ge"]):
                continue
            if req["power_le"] is not None and (power is None or power > req["power_le"]):
                continue
            if req["toughness_ge"] is not None and (toughness is None or toughness < req["toughness_ge"]):
                continue
            if req["toughness_le"] is not None and (toughness is None or toughness > req["toughness_le"]):
                continue

            matches.append(card)
        return matches

    @staticmethod
    def _match_stack_targets(
        stack: List[Dict[str, Any]],
        local_seat: int,
        opponent_seat: int | None,
        req: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        for obj in stack:
            controller = obj.get("controller_seat_id") or obj.get("owner_seat_id")
            if req["must_control"] == "you" and controller != local_seat:
                continue
            if req["must_control"] == "opponent" and opponent_seat is not None and controller != opponent_seat:
                continue

            name = (obj.get("name") or "").lower()
            type_line = (obj.get("type_line") or "").lower()

            is_ability = "ability of" in name or "ability" in type_line
            is_spell = not is_ability

            if req["target_spell"] and not is_spell:
                continue
            if req["target_ability"] and not is_ability:
                continue
            if req["types"]:
                type_match = any(t in type_line for t in req["types"])
                if not type_match:
                    continue

            matches.append(obj)
        return matches

    @staticmethod
    def _match_graveyard_targets(
        graveyard: List[Dict[str, Any]],
        local_seat: int,
        opponent_seat: int | None,
        req: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        matches = []
        for card in graveyard:
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if req["must_control"] == "you" and controller != local_seat:
                continue
            if req["must_control"] == "opponent" and opponent_seat is not None and controller != opponent_seat:
                continue

            type_line = (card.get("type_line") or "").lower()
            if req["nonland_only"] and "land" in type_line:
                continue
            if req["types"]:
                type_match = any(t in type_line for t in req["types"])
                if not type_match:
                    continue
            matches.append(card)
        return matches

    @staticmethod
    def _score_target(card: Dict[str, Any], prefer_opponent: bool) -> int:
        type_line = (card.get("type_line") or "").lower()
        power = card.get("power") or 0
        toughness = card.get("toughness") or 0
        score = 0
        if "planeswalker" in type_line:
            score += 5
        if "creature" in type_line:
            score += 2
        if "land" in type_line and not card.get("is_tapped"):
            score += 2
        if "flying" in (card.get("oracle_text") or "").lower():
            score += 1
        score += int(power) + int(toughness)
        if prefer_opponent:
            score += 1
        return score

    @staticmethod
    def _extract_explicit_target_instance_ids(decision_context: Dict[str, Any]) -> List[int]:
        """Extract legal target instance ids from bridge-enriched decision context."""
        ids: list[int] = []

        def _collect(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, int):
                ids.append(value)
                return
            if isinstance(value, list):
                for item in value:
                    _collect(item)
                return
            if isinstance(value, dict):
                for key in (
                    "instanceId",
                    "instance_id",
                    "targetInstanceId",
                    "target_instance_id",
                    "cardInstanceId",
                    "objectInstanceId",
                ):
                    raw = value.get(key)
                    if isinstance(raw, int):
                        ids.append(raw)
                        return
                for key in ("target", "card", "object"):
                    child = value.get(key)
                    if isinstance(child, (dict, list, int)):
                        _collect(child)

        for key in (
            "validTargets",
            "qualifiedTargets",
            "targetsToSelect",
            "options",
        ):
            _collect(decision_context.get(key))

        deduped: list[int] = []
        seen: set[int] = set()
        for instance_id in ids:
            if instance_id > 0 and instance_id not in seen:
                seen.add(instance_id)
                deduped.append(instance_id)
        return deduped

    @staticmethod
    def _lookup_target_cards_by_instance_ids(
        game_state: Dict[str, Any],
        instance_ids: List[int],
    ) -> List[Dict[str, Any]]:
        """Resolve legal target instance ids to visible objects across zones."""
        if not instance_ids:
            return []

        zones: list[Any] = [
            game_state.get("battlefield", []),
            game_state.get("stack", []),
            game_state.get("graveyard", []),
            game_state.get("exile", []),
        ]
        by_id: Dict[int, Dict[str, Any]] = {}
        for zone in zones:
            if not isinstance(zone, list):
                continue
            for item in zone:
                if not isinstance(item, dict):
                    continue
                instance_id = item.get("instance_id")
                if isinstance(instance_id, int):
                    by_id[instance_id] = item

        resolved: List[Dict[str, Any]] = []
        for instance_id in instance_ids:
            card = by_id.get(instance_id)
            if card is not None:
                resolved.append(card)
        return resolved

    @staticmethod
    def _get_target_selection_actions(game_state: Dict[str, Any]) -> List[str]:
        decision_context = game_state.get("decision_context") or {}
        if decision_context.get("type") != "target_selection":
            return []

        source_id = decision_context.get("source_id")
        source_card = decision_context.get("source_card") or "spell"
        source_oracle = str(
            decision_context.get("source_oracle_text")
            or decision_context.get("source_card_oracle_text")
            or ""
        )
        for obj in game_state.get("stack", []):
            if obj.get("instance_id") == source_id:
                source_oracle = source_oracle or obj.get("oracle_text", "")
                source_card = decision_context.get("source_card") or obj.get("name", source_card)
                break

        req = RulesEngine._infer_target_requirements(source_oracle)

        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        if not local_player:
            return [f"Select target for {source_card}"]
        local_seat = local_player.get("seat_id")
        opponent_player = next((p for p in players if not p.get("is_local")), None)
        opponent_seat = opponent_player.get("seat_id") if opponent_player else None

        actions = []
        if req["player_target"]:
            if req["must_control"] == "opponent":
                actions.append("Select target: Opponent")
            elif req["must_control"] == "you":
                actions.append("Select target: You")
            else:
                actions.extend(["Select target: Opponent", "Select target: You"])

        explicit_target_ids = RulesEngine._extract_explicit_target_instance_ids(decision_context)
        matches = RulesEngine._lookup_target_cards_by_instance_ids(game_state, explicit_target_ids)
        if not req["zones"] or "battlefield" in req["zones"]:
            battlefield = game_state.get("battlefield", [])
            if not matches:
                matches.extend(
                    RulesEngine._match_battlefield_targets(
                        battlefield, local_seat, opponent_seat, req
                    )
                )

        if "stack" in req["zones"]:
            stack = game_state.get("stack", [])
            if not matches:
                matches.extend(
                    RulesEngine._match_stack_targets(stack, local_seat, opponent_seat, req)
                )

        if "graveyard" in req["zones"]:
            graveyard = game_state.get("graveyard", [])
            if not matches:
                matches.extend(
                    RulesEngine._match_graveyard_targets(
                        graveyard, local_seat, opponent_seat, req
                    )
                )

        if matches:
            prefer_opponent = req["must_control"] in (None, "opponent")
            matches.sort(
                key=lambda c: RulesEngine._score_target(c, prefer_opponent),
                reverse=True,
            )
            names = [c.get("name", "Unknown") for c in matches[:3]]
            for name in RulesEngine._disambiguate_names(names):
                actions.append(f"Select target: {name}")

        if not actions:
            actions.append(f"Select target for {source_card}")
        return actions

    @staticmethod
    def _filter_legal_attackers(
        game_state: Dict[str, Any], legal_attackers: List[str]
    ) -> List[str]:
        """Filter declared attackers against visible battlefield legality."""
        if not legal_attackers:
            return []

        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        if not local_player:
            return legal_attackers

        local_seat = local_player.get("seat_id")
        turn_num = game_state.get("turn", {}).get("turn_number", 0)
        valid_name_counts: Dict[str, int] = {}
        saw_local_creature = False

        for card in game_state.get("battlefield", []):
            controller = card.get("controller_seat_id")
            owner = card.get("owner_seat_id")
            if controller not in (None, local_seat) and owner != local_seat:
                continue
            type_line = (card.get("type_line") or "").lower()
            if "creature" not in type_line:
                continue
            saw_local_creature = True
            if card.get("is_tapped"):
                continue
            if (
                card.get("turn_entered_battlefield", -1) == turn_num
                and "haste" not in (card.get("oracle_text") or "").lower()
            ):
                continue
            name = card.get("name")
            if not name:
                continue
            valid_name_counts[name] = valid_name_counts.get(name, 0) + 1

        if not saw_local_creature:
            return legal_attackers

        filtered: List[str] = []
        for name in legal_attackers:
            if valid_name_counts.get(name, 0) > 0:
                filtered.append(name)
                valid_name_counts[name] -= 1
        return filtered

    @staticmethod
    def _get_decision_actions(game_state: Dict[str, Any]) -> List[str]:
        """Compute legal actions for pending GRE decision types."""
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")
        if not dec_type:
            return []

        if dec_type == "declare_attackers":
            legal = RulesEngine._filter_legal_attackers(
                game_state, decision_context.get("legal_attackers", [])
            )
            actions = []
            if legal:
                # Pair each legal attacker name with the P/T of a matching
                # creature on the battlefield. When two creatures share a
                # name, the #1/#2 disambiguation is assigned in the same
                # order we iterate — callers see e.g.
                # "Attack with: Filcher #1 (1/3)" vs "Attack with: Filcher #2 (0/2)"
                # so the planner can distinguish a useful attacker from a
                # dead-weight one. 0-power creatures are flagged explicitly.
                local_seat = None
                for p in game_state.get("players", []):
                    if p.get("is_local"):
                        local_seat = p.get("seat_id")
                        break
                turn_num = game_state.get("turn", {}).get("turn_number", 0)
                candidates_by_name: Dict[str, List[Dict[str, Any]]] = {}
                for card in game_state.get("battlefield", []):
                    controller = card.get("controller_seat_id")
                    owner = card.get("owner_seat_id")
                    if controller not in (None, local_seat) and owner != local_seat:
                        continue
                    type_line = (card.get("type_line") or "").lower()
                    if "creature" not in type_line:
                        continue
                    if card.get("is_tapped"):
                        continue
                    if (
                        card.get("turn_entered_battlefield", -1) == turn_num
                        and "haste" not in (card.get("oracle_text") or "").lower()
                    ):
                        continue
                    name = card.get("name")
                    if name:
                        candidates_by_name.setdefault(name, []).append(card)

                disambiguated = RulesEngine._disambiguate_names(legal)
                consumed: Dict[str, int] = {}
                for display_name, bare_name in zip(disambiguated, legal):
                    queue = candidates_by_name.get(bare_name, [])
                    idx = consumed.get(bare_name, 0)
                    suffix = ""
                    if idx < len(queue):
                        card = queue[idx]
                        consumed[bare_name] = idx + 1
                        power = card.get("power")
                        toughness = card.get("toughness")
                        if power not in (None, "") and toughness not in (None, ""):
                            try:
                                power_val = int(power)
                            except (TypeError, ValueError):
                                power_val = None
                            suffix = f" ({power}/{toughness})"
                            if power_val == 0:
                                oracle = (card.get("oracle_text") or "").lower()
                                # Attack-triggered abilities can still make
                                # a 0-power attacker worthwhile (raid, exert,
                                # "whenever ~ attacks" clauses).
                                attack_trigger = (
                                    "whenever" in oracle and "attack" in oracle
                                ) or "exert" in oracle
                                if not attack_trigger:
                                    suffix += " [0 POWER — attacking deals 0 damage]"
                    actions.append(f"Attack with: {display_name}{suffix}")
            actions.append("Done (confirm attackers)")
            return actions

        if dec_type == "declare_blockers":
            legal = decision_context.get("legal_blockers", [])
            actions = []
            if legal:
                for name in RulesEngine._disambiguate_names(legal):
                    actions.append(f"Block with: {name}")
            actions.append("Done (confirm blockers)")
            return actions

        if dec_type == "assign_damage":
            return ["Assign damage (order targets by priority)", "Done"]

        if dec_type == "order_combat_damage":
            return ["Order damage targets by priority", "Done"]

        if dec_type == "pay_costs":
            source = decision_context.get("source_card", "spell")
            return [f"Pay costs for {source}", "Auto-pay"]

        if dec_type == "search":
            return ["Search library (pick best card)", "Cancel search"]

        if dec_type == "distribution":
            source = decision_context.get("source_card", "effect")
            total = decision_context.get("total", "?")
            return [f"Distribute {total} from {source}", "Done"]

        if dec_type == "numeric_input":
            source = decision_context.get("source_card", "effect")
            min_v = decision_context.get("min", 0)
            max_v = decision_context.get("max", "?")
            return [f"Choose number ({min_v}-{max_v}) for {source}"]

        if dec_type == "choose_starting_player":
            return ["Choose: Play", "Choose: Draw"]

        if dec_type == "select_replacement":
            return ["Select replacement effect order", "Done"]

        if dec_type == "casting_time_options":
            return ["Cast normally", "Use alternative cost (Foretell/Flashback/Escape)"]

        if dec_type == "select_counters":
            return ["Select counters", "Done"]

        if dec_type == "order_triggers":
            return ["Order triggered abilities", "Done"]

        if dec_type in ("select_n_group", "select_from_groups", "search_from_groups", "gather"):
            return ["Select from options", "Done"]

        if dec_type == "optional_action":
            # MTGA is presenting a yes/no prompt (e.g. "Send your commander to
            # the command zone instead of the graveyard?"). Without these,
            # get_legal_actions() falls through to the priority check and
            # returns "Wait (Opponent has priority)" — which is wrong because
            # the local player is the one being asked, and the planner ends up
            # passing priority on a request type that doesn't accept a pass.
            return ["Accept (yes)", "Decline (no)"]

        return []

    @staticmethod
    def get_legal_actions(game_state: Dict[str, Any]) -> List[str]:
        target_actions = RulesEngine._get_target_selection_actions(game_state)
        if target_actions:
            return target_actions

        # Decision windows must override any stale ActionsAvailable list.
        decision_actions = RulesEngine._get_decision_actions(game_state)
        if decision_actions:
            return decision_actions

        # PREFERENCE: Use ground-truth legal actions from GRE if available
        if game_state.get("legal_actions"):
            return game_state["legal_actions"]

        actions = []

        turn = game_state.get("turn", {})
        phase = turn.get("phase", "")

        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        if not local_player:
            return ["Wait (Game State Syncing)"]

        local_seat = local_player.get("seat_id")
        is_active_player = (turn.get("active_player") == local_seat)
        has_priority = (turn.get("priority_player") == local_seat)

        if not has_priority:
             # Exception: We can declare blockers if it's the DeclareBlock step and we are defender
             step = turn.get("step", "")
             is_blocking_step = (step == "Step_DeclareBlock") and (not is_active_player)

             if not is_blocking_step:
                 return ["Wait (Opponent has priority)"]

        # Calculate available mana (with color breakdown)
        mana_pool = RulesEngine._get_mana_pool(game_state, local_seat)

        # 1. LAND DROPS
        # Legal if: Main Phase, Stack Empty, Lands Played < 1, Active Player
        stack = game_state.get("stack", [])
        is_stack_empty = len(stack) == 0
        is_main_phase = "Main" in phase
        
        if is_active_player and is_main_phase and is_stack_empty:
             if local_player.get("lands_played", 0) < 1:
                # Check hand for lands
                hand = game_state.get("hand", [])
                for card in hand:
                    if "Land" in card.get("type_line", ""):
                        actions.append(f"Play Land: {card.get('name')}")
                        # We only need to list one land action generally, or all specific ones?
                        # Let's list specific logic.

        # Pre-fetch battlefield for Aura target checks and attacker/blocker logic
        battlefield = game_state.get("battlefield", [])

        # 2. CASTING
        # Sorcery Speed: Main Phase, Stack Empty, Active Player
        # Instant Speed: Anytime we have priority
        hand = game_state.get("hand", [])
        for card in hand:
            type_line = card.get("type_line", "")
            name = card.get("name", "")

            # Skip lands handled above
            if "Land" in type_line:
                continue

            is_instant_speed = "Instant" in type_line or "Flash" in card.get("oracle_text", "")

            can_cast_timing = False
            if is_instant_speed:
                can_cast_timing = True
            elif is_active_player and is_main_phase and is_stack_empty:
                can_cast_timing = True

            # Mana check: ensure player can afford the spell (total + colors)
            can_afford = RulesEngine._can_afford(card.get("mana_cost", ""), mana_pool)

            if can_cast_timing and can_afford:
                # Aura target check: Auras require a valid target to cast.
                # Beneficial Auras (+X/+X, keyword grants) need a friendly creature;
                # detrimental Auras (Pacifism, -X/-X, "can't") need an enemy creature.
                if "Aura" in type_line:
                    oracle = card.get("oracle_text", "").lower()
                    if "enchant creature" in oracle:
                        my_creatures = [
                            c for c in battlefield
                            if c.get("owner_seat_id") == local_seat
                            and "Creature" in c.get("type_line", "")
                        ]
                        opp_creatures = [
                            c for c in battlefield
                            if c.get("owner_seat_id") != local_seat
                            and "Creature" in c.get("type_line", "")
                        ]
                        # Heuristic: detrimental if it weakens or restricts the target
                        is_detrimental = any(
                            kw in oracle
                            for kw in ("-1/", "-2/", "-3/", "-4/", "can't attack", "can't block",
                                        "doesn't untap", "sacrifice enchanted")
                        )
                        if is_detrimental:
                            if not opp_creatures:
                                continue  # No enemy targets for detrimental Aura
                        else:
                            if not my_creatures:
                                continue  # No friendly targets for beneficial Aura

                # Non-Aura targeted removal: spells/enchantments that exile or
                # destroy a target opponent's permanent need a valid target.
                # Without this check the RulesEngine suggests cards like
                # Seam Rip when the opponent has no valid targets.
                oracle = card.get("oracle_text", "").lower()
                if "target" in oracle and "opponent controls" in oracle:
                    opp_nonlands = [
                        c for c in battlefield
                        if c.get("owner_seat_id") != local_seat
                        and "Land" not in c.get("type_line", "")
                    ]
                    if not opp_nonlands:
                        continue  # No valid opponent targets

                actions.append(f"Cast {name}")

        # 3. ATTACKING
        # Legal if: Combat Phase (specifically Declare Attackers step?), Active Player, Creatures Untapped + !Sick
        # In Arena, we usually get priority *before* attackers are declared (Beginning of Combat) 
        # or *during* declare attackers (if we hold priority, but usually it's a game step).
        # Actually, asking "Who should attack" happens at 'Phase_Combat_Beginning' or 'Phase_Main1' (planning).
        
        my_creatures = [c for c in battlefield if c.get("owner_seat_id") == local_seat and "Creature" in c.get("type_line", "")]

        if is_active_player and ("Main" in phase or "Combat" in phase):
             potential_attackers = []
             turn_num = turn.get("turn_number", 0)
             
             # Safe turn parse
             try:
                 current_turn_int = int(str(turn_num).replace("?", "0"))
             except:
                 current_turn_int = 0

             for c in my_creatures:
                 # Check Sickness
                 entered = c.get("turn_entered_battlefield", -1)
                 has_haste = "haste" in c.get("oracle_text", "").lower()
                 is_tapped = c.get("is_tapped", False)

                 is_sick = (entered == current_turn_int) and not has_haste

                 if not is_sick and not is_tapped:
                     potential_attackers.append(c.get("name"))

             if potential_attackers:
                 actions.append(f"Declare Attackers: {', '.join(RulesEngine._disambiguate_names(potential_attackers))}")
        
        # 4. BLOCKING
        # Legal if: Combat Phase, Defending Player
        if not is_active_player and "Combat" in phase:
             untapped_blockers = [c.get("name") for c in my_creatures if not c.get("is_tapped")]
             if untapped_blockers:
                 actions.append(f"Block with: {', '.join(RulesEngine._disambiguate_names(untapped_blockers))}")

        # 5. ABILITIES
        # Activated abilities on battlefield
        turn_num = turn.get("turn_number", 0)
        try:
            current_turn_int = int(str(turn_num).replace("?", "0"))
        except Exception:
            current_turn_int = 0
        ability_names = []
        for c in my_creatures: # And lands/artifacts
            oracle = c.get("oracle_text", "")
            if ": " not in oracle:  # Crude check for activated ability
                continue
            if c.get("is_tapped"):
                continue
            # Summoning sickness: creatures can't use {T} abilities the turn they enter
            entered = c.get("turn_entered_battlefield", -1)
            has_haste = "haste" in oracle.lower()
            is_sick = (entered == current_turn_int) and not has_haste
            uses_tap = bool(re.search(r'\{T\}', oracle))
            if is_sick and uses_tap:
                continue
            ability_names.append(c.get("name"))
        for aname in RulesEngine._disambiguate_names(ability_names):
            actions.append(f"Activate {aname}")

        return actions

