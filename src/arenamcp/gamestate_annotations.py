"""GRE annotation processing mixin for GameState."""

from __future__ import annotations

import logging

from arenamcp.gamestate_models import GameObjectKind
from arenamcp.gamestate_transforms import (
    _coerce_int,
    _coerce_optional_int,
    _coerce_str_list,
    _collapse_gre_value,
    _counter_type_name,
    _ensure_dict_list,
    _ensure_int_list,
    _ensure_list,
)
from arenamcp.player_protection import PLAYER_PROTECTION_ABILITIES

logger = logging.getLogger(__name__)


class _GameStateAnnotationsMixin:
    """Processes GRE message annotations (damage, targets, zone transfers, etc.)."""

    def _record_life_gain(self, ann: dict, affected_ids: list[int], detail_map: dict) -> None:
        """Add a ModifiedLife gain to the affected seats' life-gained counters.

        The annotation's affectedIds are seat ids and its ``life`` detail the
        signed change. Each annotation id is counted once: the same id can be
        seen again in a later message.
        """
        delta = _coerce_int(detail_map.get("life", 0), 0)
        if delta <= 0:
            return
        ann_id = _coerce_optional_int(ann.get("id"))
        seen = getattr(self, "_life_annotation_ids", None)
        if seen is None:
            seen = self._life_annotation_ids = set()
        if ann_id is not None:
            if ann_id in seen:
                return
            seen.add(ann_id)
        for seat in affected_ids:
            player = self.players.get(seat)
            if player is None:
                continue
            player.life_gained_this_turn += delta
            player.life_gained_this_game += delta

    def _process_annotations(self, annotations: list[dict]) -> None:
        """Process GRE annotations from a GameStateMessage.

        Annotations carry the 'why' behind state changes: damage, counters,
        zone transfers, reveals, resolution lifecycle, etc.

        Args:
            annotations: List of annotation dicts from GameStateMessage.
        """
        ann_list = _ensure_dict_list(annotations)
        renamed_from: dict[int, int] = {}
        for ann in ann_list:
            ann_types = [str(t) for t in _ensure_list(ann.get("type", [])) if t is not None]
            if "AnnotationType_ObjectIdChanged" not in ann_types:
                continue
            detail_map = {}
            for d in _ensure_dict_list(ann.get("details", [])):
                key = d.get("key", "")
                if key:
                    raw = d.get("valueInt32")
                    if raw in (None, [], ""):
                        raw = d.get("valueString")
                    if raw in (None, [], ""):
                        raw = d.get("valueInt64")
                    detail_map[key] = _collapse_gre_value(raw)
            orig_id = _coerce_int(detail_map.get("orig_id", 0), 0)
            new_id = _coerce_int(detail_map.get("new_id", 0), 0)
            if orig_id and new_id:
                renamed_from[new_id] = orig_id

        for ann in ann_list:
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
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0) or _coerce_int(
                        ann.get("affectorId", 0), 0
                    )
                    target_id = _coerce_int(
                        detail_map.get("targetId", affected_ids[0] if affected_ids else 0),
                        affected_ids[0] if affected_ids else 0,
                    )
                    # If target is a player seat, track cumulative damage
                    for seat_id in self.players:
                        if target_id == seat_id:
                            self.damage_taken[seat_id] = self.damage_taken.get(seat_id, 0) + damage_amount
                    # Resolve source and target names for event log (do not fall back
                    # source_id to affected_ids[0], which is the damaged target — ledger #105).
                    source_obj = self.game_objects.get(source_id)
                    if source_obj:
                        # An ability source is named after its permanent, never 'Card#<ability id>'.
                        source_name = self._resolve_object_display_name(source_obj)
                    elif source_id in self.players:
                        source_name = "You" if source_id == self.local_seat_id else "Opponent"
                    else:
                        source_name = f"#{source_id}" if source_id else "unknown"
                    target_obj = self.game_objects.get(target_id)
                    if target_obj:
                        target_name = self._resolve_object_display_name(target_obj)
                    elif target_id in self.players:
                        target_name = "You" if target_id == self.local_seat_id else "Opponent"
                    else:
                        target_name = f"#{target_id}" if target_id else "unknown"
                    self._add_event(
                        {
                            "type": "damage_dealt",
                            "source": source_name,
                            "source_id": source_id,
                            "amount": damage_amount,
                            "target": target_name,
                            "target_id": target_id,
                        }
                    )

                elif ann_type == "AnnotationType_ZoneTransfer":
                    # Card moved zones (died, bounced, exiled, etc.)
                    zone_src = detail_map.get("zone_src", "")
                    zone_dest = detail_map.get("zone_dest", "")
                    category = detail_map.get("category", "")
                    for obj_id in affected_ids:
                        orig_id = renamed_from.get(obj_id, 0)
                        obj = self.game_objects.get(obj_id) or (
                            self.game_objects.get(orig_id) if orig_id else None
                        )
                        if obj:
                            if category == "PlayLand":
                                obj.entered_via_play_land = True
                            event_payload = {
                                "type": "zone_transfer",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                                "from_zone": zone_src,
                                "to_zone": zone_dest,
                                "category": category,
                                "owner_seat_id": obj.owner_seat_id,
                                "controller_seat_id": obj.controller_seat_id,
                            }
                            if orig_id:
                                event_payload["orig_instance_id"] = orig_id
                            self._add_event(event_payload)

                elif ann_type in ("AnnotationType_CounterAdded", "AnnotationType_CounterRemoved"):
                    counter_type = _counter_type_name(detail_map)
                    counter_count = _coerce_int(
                        detail_map.get(
                            "counterCount",
                            detail_map.get("transaction_amount", detail_map.get("count", 1)),
                        ),
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
                                if (
                                    counter_type == "Stun"
                                    and self._in_untap_step
                                    and self._untaps_this_step(
                                        obj.controller_seat_id or obj.owner_seat_id, obj.zone_id
                                    )
                                ):
                                    # The GRE removed a stun counter instead of
                                    # untapping it (CR 122.1d), so it stays tapped
                                    # even if the stun was never seen going on.
                                    obj.is_tapped = True
                            self._add_event(
                                {
                                    "type": "counter_added" if is_added else "counter_removed",
                                    "card": self._resolve_card_name(obj.grp_id),
                                    "instance_id": obj_id,
                                    "counter_type": counter_type,
                                    "amount": counter_count,
                                }
                            )

                elif ann_type == "AnnotationType_TappedUntappedPermanent":
                    # The GRE's own record of a tap (tapped=1) or untap (tapped=0).
                    # Player.log omits isTapped when it is false, so an untap is
                    # otherwise invisible on the object. An untap also ends any
                    # untap prevention inferred for the permanent.
                    tapped = _coerce_optional_int(detail_map.get("tapped"))
                    if tapped is not None:
                        for obj_id in affected_ids:
                            obj = self.game_objects.get(obj_id)
                            if obj:
                                obj.is_tapped = bool(tapped)
                            if not tapped:
                                self._untap_prevention.discard(obj_id)

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

                elif ann_type == "AnnotationType_LinkInfo":
                    # 77a434ba gsid120: affector231, affected seat1, choice90240.
                    # Other choose-player links do not imply protection.
                    ability = _coerce_int(detail_map.get("sourceAbilityGRPID"), 0)
                    source = self.game_objects.get(_coerce_int(ann.get("affectorId"), 0))
                    if (
                        source is not None
                        and detail_map.get("ChooseLinkType") == "GamePlayer"
                        and _coerce_int(detail_map.get("LinkType"), 0) == 3
                        and ability in PLAYER_PROTECTION_ABILITIES
                        and len(affected_ids) == 1
                        and affected_ids[0] > 0
                    ):
                        source.chosen_player_links[ability] = affected_ids[0]

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
                            card_name = self._resolve_card_name(obj.grp_id)
                            # Keep the instance and its zone at reveal time: a
                            # stand-in revealed into a hand has the hand as its
                            # zoneId; a library-search reveal has the library.
                            # Persistent reveals repeat, so the first record wins.
                            if obj_id not in self.revealed_instances:
                                zone = self.zones.get(obj.zone_id)
                                self.revealed_instances[obj_id] = {
                                    "instance_id": obj_id,
                                    "grp_id": obj.grp_id,
                                    "name": card_name,
                                    "owner_seat_id": owner,
                                    "zone_id": obj.zone_id,
                                    "zone_type": zone.zone_type.value if zone else "",
                                    "retired": False,
                                }
                            self._add_event(
                                {
                                    "type": "card_revealed",
                                    "card": card_name,
                                    "instance_id": obj_id,
                                    "owner_seat": owner,
                                }
                            )

                elif ann_type == "AnnotationType_RevealedCardDeleted":
                    # The GRE deletes the stand-in once the real card leaves
                    # the hidden zone (cast, discarded, shuffled away).
                    for obj_id in affected_ids:
                        record = self.revealed_instances.get(obj_id)
                        if record is not None:
                            record["retired"] = True

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
                    # The affector is the ability that made the token (Player.log 2026-10-19
                    # gsid 289: ability 541, grpId 152901, parentId 451 = Lagomos, Hand of
                    # Hatred). Its text says whether the token leaves at the next end step
                    # (ledger #185, token_lifetime.py); the ability object is gone soon after.
                    creator = (
                        self.game_objects.get(_coerce_int(ann.get("affectorId", 0), 0))
                        if is_created
                        else None
                    )
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and creator is not None and creator.instance_id != obj_id:
                            if creator.object_kind == GameObjectKind.ABILITY:
                                obj.token_creator_ability_grp_id = creator.grp_id or None
                                obj.token_creator_id = creator.parent_instance_id or None
                            else:
                                obj.token_creator_id = creator.instance_id
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
                    # Build a concise action history entry. The GRE writes the
                    # action type as its enum number, the acting seat as the
                    # annotation's affectorId and the card as the affected
                    # instance (bug reports 2026-10-09 showed seat 0, "3", "").
                    action_type = detail_map.get("actionType", "")
                    grp_id = _coerce_int(detail_map.get("grpId", 0), 0)
                    seat = _coerce_int(detail_map.get("seatId", 0), 0) or _coerce_int(
                        ann.get("affectorId", 0), 0
                    )
                    if not grp_id:
                        for obj_id in affected_ids:
                            obj = self.game_objects.get(obj_id)
                            if obj is not None and obj.grp_id:
                                grp_id = obj.grp_id
                                break
                    card_name = self._resolve_card_name(grp_id) if grp_id else ""
                    history_entry = {
                        "turn": self.turn_info.turn_number,
                        "phase": self.turn_info.phase,
                        "seat": seat,
                        "action": _action_type_name(action_type),
                        "card": card_name,
                        "instance_ids": list(affected_ids[:4]),
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
                    self._record_life_gain(ann, affected_ids, detail_map)
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
                    # REASON is a ReasonFaceDown value (6 = Disguise): the snapshot names it (#123).
                    reason = _coerce_optional_int(detail_map.get("REASON"))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj is not None and obj.face_down and reason is not None:
                            obj.face_down_reason = reason  # isFacedown itself says face down
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
                    # Spell/ability targeting — links source to target instance IDs.
                    # Arena names the source as the annotation's affectorId and the
                    # targets as its affectedIds (2026-10-19 match 17a32d51 T5: Shock
                    # 312 -> Emberheart 307); with only "sourceId" read, no stack
                    # object ever got its targets (ledger #122).
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0) or _coerce_int(
                        ann.get("affectorId", 0), 0
                    )
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
                    source_id = _coerce_int(detail_map.get("sourceId", 0), 0) or _coerce_int(
                        ann.get("affectorId", 0), 0
                    )
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
                    # Arena publishes the accumulated commander tax on the local
                    # player's designation, even after an instance-ID change or
                    # mid-game reconnect. Object zone transitions alone miss it.
                    if (
                        self.local_seat_id in affected_ids
                        and _coerce_int(detail_map.get("DesignationType"), 0) == 1
                    ):
                        grp_id = _coerce_int(detail_map.get("grpid"), 0)
                        tax = _coerce_optional_int(detail_map.get("CostIncrease"))
                        if grp_id > 0 and tax is not None and tax >= 0 and tax % 2 == 0:
                            self.commander_casts[grp_id] = tax // 2
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


# GreProtobuf ActionType (re-output/GreProtobuf/.../ActionType.cs): the log
# carries the number, the bridge the name.
_ACTION_TYPE_NAMES = {
    0: "None", 1: "Cast", 2: "Activate", 3: "Play", 4: "ActivateMana", 5: "Pass", 6: "ActivateTest",
    7: "Special", 8: "SpecialTurnFaceUp", 9: "ResolutionCost", 10: "CastLeft", 11: "CastRight",
    12: "MakePayment", 14: "CombatCost", 15: "OpeningHandAction", 16: "CastAdventure", 17: "FloatMana",
    18: "CastMdfc", 19: "PlayMdfc", 20: "SpecialPayment",
}  # fmt: skip


def _action_type_name(value: object) -> str:
    """'Play' for 3, 'ActionType_Play' or 'Play'; 'unknown' for nothing."""
    if value in (None, "", []):
        return "unknown"
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return _ACTION_TYPE_NAMES.get(int(value), f"ActionType_{value}")
    return str(value).replace("ActionType_", "")
