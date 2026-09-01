"""GRE annotation processing mixin for GameState."""

from __future__ import annotations

import logging

from arenamcp.gamestate_transforms import (
    _coerce_int,
    _coerce_optional_int,
    _coerce_str_list,
    _collapse_gre_value,
    _ensure_dict_list,
    _ensure_int_list,
    _ensure_list,
)

logger = logging.getLogger(__name__)


class _GameStateAnnotationsMixin:
    """Processes GRE message annotations (damage, targets, zone transfers, etc.)."""

    def _process_annotations(self, annotations: list[dict]) -> None:
        """Process GRE annotations from a GameStateMessage.

        Annotations carry the 'why' behind state changes: damage, counters,
        zone transfers, reveals, resolution lifecycle, etc.

        Args:
            annotations: List of annotation dicts from GameStateMessage.
        """
        for ann in _ensure_dict_list(annotations):
            ann_types = [str(t) for t in _ensure_list(ann.get("type", [])) if t is not None]
            details = _ensure_dict_list(ann.get("details", []))
            affected_ids = _ensure_int_list(ann.get("affectedIds", []))
            # Build a quick detail lookup
            detail_map = {}
            for d in details:
                key = d.get("key", "")
                if key:
                    raw = d.get("valueInt32")
                    if raw in (None, [], ""):
                        raw = d.get("valueString")
                    if raw in (None, [], ""):
                        raw = d.get("valueInt64")
                    detail_map[key] = _collapse_gre_value(raw)

            for ann_type in ann_types:
                if ann_type == "AnnotationType_DamageDealt":
                    # Track damage: who dealt how much to whom
                    damage_amount = _coerce_int(detail_map.get("damage", 0), 0)
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0)
                    target_id = _coerce_int(
                        detail_map.get("targetId", affected_ids[0] if affected_ids else 0),
                        affected_ids[0] if affected_ids else 0,
                    )
                    # If target is a player seat, track cumulative damage
                    for seat_id in self.players:
                        if target_id == seat_id:
                            self.damage_taken[seat_id] = self.damage_taken.get(seat_id, 0) + damage_amount
                    # Resolve source card name for event log
                    # sourceId=0 means the detail didn't include the source —
                    # fall back to affected_ids (the objects involved in the damage)
                    if not source_id and affected_ids:
                        source_id = affected_ids[0]
                    source_obj = self.game_objects.get(source_id)
                    source_name = (
                        self._resolve_card_name(source_obj.grp_id)
                        if source_obj
                        else (f"#{source_id}" if source_id else "unknown")
                    )
                    self._add_event(
                        {
                            "type": "damage_dealt",
                            "source": source_name,
                            "source_id": source_id,
                            "amount": damage_amount,
                            "target_id": target_id,
                        }
                    )

                elif ann_type == "AnnotationType_ZoneTransfer":
                    # Card moved zones (died, bounced, exiled, etc.)
                    zone_src = detail_map.get("zone_src", "")
                    zone_dest = detail_map.get("zone_dest", "")
                    category = detail_map.get("category", "")
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            if category == "PlayLand":
                                obj.entered_via_play_land = True
                            self._add_event(
                                {
                                    "type": "zone_transfer",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                    "from_zone": zone_src,
                                    "to_zone": zone_dest,
                                    "category": category,
                                }
                            )

                elif ann_type in ("AnnotationType_CounterAdded", "AnnotationType_CounterRemoved"):
                    counter_type = detail_map.get("counterType", "unknown")
                    counter_count = _coerce_int(
                        detail_map.get("counterCount", detail_map.get("count", 1)),
                        1,
                    )
                    is_added = "Added" in ann_type
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            # Update counter state on the object
                            current = obj.counters.get(counter_type, 0)
                            if is_added:
                                obj.counters[counter_type] = current + counter_count
                            else:
                                obj.counters[counter_type] = max(0, current - counter_count)
                                if obj.counters[counter_type] == 0:
                                    del obj.counters[counter_type]
                            self._add_event(
                                {
                                    "type": "counter_added" if is_added else "counter_removed",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                    "counter_type": counter_type,
                                    "amount": counter_count,
                                }
                            )

                elif ann_type == "AnnotationType_ControllerChanged":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        new_controller = _coerce_int(detail_map.get("controllerId", 0), 0)
                        if obj:
                            self._add_event(
                                {
                                    "type": "controller_changed",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                    "new_controller": new_controller,
                                }
                            )

                elif ann_type in (
                    "AnnotationType_CardRevealed",
                    "AnnotationType_InstanceRevealedToOpponent",
                    "AnnotationType_RevealedCardCreated",
                ):
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and obj.grp_id:
                            owner = obj.owner_seat_id
                            if owner not in self.revealed_cards:
                                self.revealed_cards[owner] = set()
                            self.revealed_cards[owner].add(obj.grp_id)
                            self._add_event(
                                {
                                    "type": "card_revealed",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                    "owner_seat": owner,
                                }
                            )

                elif ann_type == "AnnotationType_ResolutionStart":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event(
                                {
                                    "type": "resolution_start",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                }
                            )

                elif ann_type == "AnnotationType_ResolutionComplete":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event(
                                {
                                    "type": "resolution_complete",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                }
                            )

                elif ann_type in ("AnnotationType_TokenCreated", "AnnotationType_TokenDeleted"):
                    is_created = "Created" in ann_type
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event(
                                {
                                    "type": "token_created" if is_created else "token_deleted",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                }
                            )

                elif ann_type == "AnnotationType_TriggeringObject":
                    # Links a triggered ability to its source
                    source_id = _coerce_int(
                        detail_map.get("sourceId", affected_ids[0] if affected_ids else 0),
                        affected_ids[0] if affected_ids else 0,
                    )
                    trigger_id = _coerce_int(detail_map.get("triggerId", 0), 0)
                    source_obj = self.game_objects.get(source_id)
                    if source_obj:
                        self._add_event(
                            {
                                "type": "trigger",
                                "source": self._resolve_card_name(source_obj.grp_id),
                                "source_id": source_id,
                                "trigger_id": trigger_id,
                            }
                        )

                elif ann_type == "AnnotationType_ManaPaid":
                    # Mana payment details
                    self._add_event(
                        {
                            "type": "mana_paid",
                            "details": detail_map,
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_UserActionTaken":
                    # Player took an action — also record in action history buffer
                    event = {
                        "type": "user_action",
                        "details": detail_map,
                        "affected_ids": affected_ids,
                    }
                    self._add_event(event)
                    # Build a concise action history entry
                    action_type = detail_map.get("actionType", "")
                    grp_id = _coerce_int(detail_map.get("grpId", 0), 0)
                    seat = _coerce_int(detail_map.get("seatId", 0), 0)
                    card_name = self._resolve_card_name(grp_id) if grp_id else ""
                    history_entry = {
                        "turn": self.turn_info.turn_number,
                        "phase": self.turn_info.phase,
                        "seat": seat,
                        "action": str(action_type).replace("ActionType_", "") if action_type else "unknown",
                        "card": card_name,
                    }
                    self.action_history.append(history_entry)
                    # Cap at 50 entries
                    if len(self.action_history) > 50:
                        self.action_history = self.action_history[-50:]

                elif ann_type == "AnnotationType_Scry":
                    self._add_event(
                        {
                            "type": "scry",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type in ("AnnotationType_LossOfGame", "AnnotationType_WinTheGame"):
                    result = self._resolve_end_annotation_locked(ann_type, affected_ids)
                    self._add_event(
                        {
                            "type": "game_end",
                            "result": result or "unknown",
                            "affected_ids": affected_ids,
                        }
                    )
                    # Persist result so it survives reset() for post-match analysis
                    self._record_match_result_locked(result, ann_type)

                elif ann_type == "AnnotationType_ModifiedLife":
                    self._add_event(
                        {
                            "type": "life_changed",
                            "details": detail_map,
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type in ("AnnotationType_CoinFlip", "AnnotationType_ChoiceResult"):
                    self._add_event(
                        {
                            "type": "random_result",
                            "sub_type": ann_type.replace("AnnotationType_", ""),
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_FaceDown":
                    for obj_id in affected_ids:
                        self._add_event(
                            {
                                "type": "face_down",
                                "instance_id": obj_id,
                            }
                        )

                elif ann_type in ("AnnotationType_CreateAttachment", "AnnotationType_AttachmentCreated"):
                    self._add_event(
                        {
                            "type": "attachment",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                # ── Phase 1 turbo-charge: new annotation handlers ──

                elif ann_type == "AnnotationType_TargetSpec":
                    # Spell/ability targeting — links source to target instance IDs
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0)
                    target_ids = _ensure_int_list(detail_map.get("targetIds", affected_ids))
                    source_obj = self.game_objects.get(source_id)
                    if source_obj:
                        source_obj.targeting = list(target_ids)
                    target_names = []
                    for tid in target_ids:
                        tobj = self.game_objects.get(tid)
                        if tobj:
                            target_names.append(self._resolve_card_name(tobj.grp_id))
                        else:
                            # Target might be a player seat
                            target_names.append(f"Player#{tid}" if tid in self.players else f"#{tid}")
                    self._add_event(
                        {
                            "type": "target_spec",
                            "source": self._resolve_card_name(source_obj.grp_id)
                            if source_obj
                            else f"#{source_id}",
                            "source_id": source_id,
                            "targets": target_names,
                            "target_ids": list(target_ids),
                        }
                    )

                elif ann_type == "AnnotationType_PredictedDirectDamage":
                    # GRE's own combat damage prediction
                    self._add_event(
                        {
                            "type": "predicted_damage",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_ModifiedPower":
                    # Actual power after continuous effects
                    new_power = detail_map.get("value", detail_map.get("power"))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        new_power_int = _coerce_optional_int(new_power)
                        if obj and new_power_int is not None:
                            obj.modified_power = new_power_int

                elif ann_type == "AnnotationType_ModifiedCost":
                    cost_str = detail_map.get("value", detail_map.get("cost", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and cost_str:
                            obj.modified_cost = str(cost_str)

                elif ann_type == "AnnotationType_ModifiedColor":
                    colors = _coerce_str_list(detail_map.get("colors", detail_map.get("value", [])))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and colors:
                            obj.modified_colors = list(colors)

                elif ann_type == "AnnotationType_ModifiedType":
                    types = _coerce_str_list(detail_map.get("types", detail_map.get("value", [])))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and types:
                            obj.modified_types = list(types)

                elif ann_type == "AnnotationType_ModifiedName":
                    name = detail_map.get("value", detail_map.get("name", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and name:
                            obj.modified_name = str(name)

                elif ann_type == "AnnotationType_LayeredEffect":
                    # Active continuous effect (anthem, debuff, etc.)
                    self._add_event(
                        {
                            "type": "layered_effect",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type in ("AnnotationType_AddAbility", "AnnotationType_DynamicAbility"):
                    ability = detail_map.get("abilityGrpId", detail_map.get("ability", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and ability:
                            ability_str = str(ability)
                            if ability_str not in obj.granted_abilities:
                                obj.granted_abilities.append(ability_str)
                    self._add_event(
                        {
                            "type": "ability_added",
                            "affected_ids": affected_ids,
                            "ability": str(ability),
                        }
                    )

                elif ann_type == "AnnotationType_RemoveAbility":
                    ability = detail_map.get("abilityGrpId", detail_map.get("ability", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and ability:
                            ability_str = str(ability)
                            if ability_str not in obj.removed_abilities:
                                obj.removed_abilities.append(ability_str)
                    self._add_event(
                        {
                            "type": "ability_removed",
                            "affected_ids": affected_ids,
                            "ability": str(ability),
                        }
                    )

                elif ann_type == "AnnotationType_DamagedThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.damaged_this_turn = True

                elif ann_type == "AnnotationType_CrewedThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.crewed_this_turn = True

                elif ann_type == "AnnotationType_SaddledThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.saddled_this_turn = True

                elif ann_type == "AnnotationType_PhasedOut":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.is_phased_out = True
                    self._add_event(
                        {
                            "type": "phased_out",
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_PhasedIn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.is_phased_out = False
                    self._add_event(
                        {
                            "type": "phased_in",
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_ClassLevel":
                    level = detail_map.get("level", detail_map.get("value", 1))
                    level_int = _coerce_optional_int(level)
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and level_int is not None:
                            obj.class_level = level_int
                    self._add_event(
                        {
                            "type": "class_level",
                            "affected_ids": affected_ids,
                            "level": level_int if level_int is not None else level,
                        }
                    )

                elif ann_type == "AnnotationType_DungeonStatus":
                    dungeon = detail_map.get("dungeon", "")
                    room = detail_map.get("room", "")
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        owner = obj.owner_seat_id if obj else (affected_ids[0] if affected_ids else 0)
                        if owner:
                            self.dungeon_status[owner] = {"dungeon": dungeon, "room": room}
                    self._add_event(
                        {
                            "type": "dungeon_status",
                            "dungeon": dungeon,
                            "room": room,
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_SuspendLike":
                    # Cards in exile with time counters (suspend, foretell, etc.)
                    self._add_event(
                        {
                            "type": "suspend_like",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type in ("AnnotationType_LinkedDamage", "AnnotationType_DamageSource"):
                    # Damage attribution — which source dealt what
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0)
                    source_obj = self.game_objects.get(source_id)
                    self._add_event(
                        {
                            "type": "damage_attribution",
                            "sub_type": ann_type.replace("AnnotationType_", ""),
                            "source": self._resolve_card_name(source_obj.grp_id)
                            if source_obj
                            else f"#{source_id}",
                            "source_id": source_id,
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_SupplementalText":
                    self._add_event(
                        {
                            "type": "supplemental_text",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_ColorProduction":
                    colors = _coerce_str_list(detail_map.get("colors", detail_map.get("value", [])))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and colors:
                            obj.color_production = list(colors)

                elif ann_type == "AnnotationType_CopiedObject":
                    source_grp = _coerce_optional_int(
                        detail_map.get("sourceGrpId", detail_map.get("grpId", 0))
                    )
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and source_grp is not None:
                            obj.copied_from_grp_id = source_grp
                    self._add_event(
                        {
                            "type": "copied_object",
                            "affected_ids": affected_ids,
                            "source_grp_id": source_grp,
                        }
                    )

                elif ann_type in ("AnnotationType_Designation", "AnnotationType_GainDesignation"):
                    designation = detail_map.get("designation", detail_map.get("value", ""))
                    for obj_id in affected_ids:
                        # Designations apply to players (seat IDs)
                        seat = obj_id if obj_id in self.players else None
                        if seat is None:
                            obj = self.game_objects.get(obj_id)
                            seat = obj.controller_seat_id if obj else None
                        if seat:
                            if seat not in self.designations:
                                self.designations[seat] = set()
                            self.designations[seat].add(str(designation))
                    self._add_event(
                        {
                            "type": "designation_gained",
                            "designation": designation,
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_LoseDesignation":
                    designation = detail_map.get("designation", detail_map.get("value", ""))
                    for obj_id in affected_ids:
                        seat = obj_id if obj_id in self.players else None
                        if seat is None:
                            obj = self.game_objects.get(obj_id)
                            seat = obj.controller_seat_id if obj else None
                        if seat and seat in self.designations:
                            self.designations[seat].discard(str(designation))
                    self._add_event(
                        {
                            "type": "designation_lost",
                            "designation": designation,
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_BoonInfo":
                    self._add_event(
                        {
                            "type": "boon",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_Vote":
                    self._add_event(
                        {
                            "type": "vote",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_Shuffle":
                    self._add_event(
                        {
                            "type": "shuffle",
                            "affected_ids": affected_ids,
                        }
                    )

                elif ann_type == "AnnotationType_DieRoll":
                    self._add_event(
                        {
                            "type": "die_roll",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_ObjectIdChanged":
                    self._add_event(
                        {
                            "type": "object_id_changed",
                            "affected_ids": affected_ids,
                            "details": detail_map,
                        }
                    )

                elif ann_type == "AnnotationType_NewTurnStarted":
                    # Reset turn-specific flags on all game objects
                    for obj in self.game_objects.values():
                        obj.damaged_this_turn = False
                        obj.crewed_this_turn = False
                        obj.saddled_this_turn = False
                        obj.targeting.clear()

                elif ann_type == "AnnotationType_LoopCount":
                    self._mark_engine_busy(
                        "loop_count",
                        detail_map,
                        affected_ids,
                        duration=2.0,
                    )

                elif ann_type == "AnnotationType_SyntheticEvent":
                    self._mark_engine_busy(
                        "synthetic_event",
                        detail_map,
                        affected_ids,
                        duration=1.5,
                    )

                elif ann_type in (
                    "AnnotationType_None",
                    "AnnotationType_Attachment",
                    "AnnotationType_ObjectsSelected",
                    "AnnotationType_PendingEffect",
                    "AnnotationType_Qualification",
                    "AnnotationType_Haunt",
                    "AnnotationType_GroupedIds",
                    "AnnotationType_TurnPermanent",
                    "AnnotationType_LinkInfo",
                    "AnnotationType_CopyException",
                    "AnnotationType_AbilityExhausted",
                    "AnnotationType_ManaDetails",
                    "AnnotationType_RemoveAttachment",
                    "AnnotationType_ShouldntPlay",
                    "AnnotationType_TextChange",
                    "AnnotationType_AssignDamageConfirmation",
                ):
                    # Known but not actionable for coaching — skip silently
                    pass

                else:
                    logger.debug(
                        "Unhandled annotation type: %s (affected: %s, details: %s)",
                        ann_type,
                        affected_ids,
                        detail_map,
                    )
