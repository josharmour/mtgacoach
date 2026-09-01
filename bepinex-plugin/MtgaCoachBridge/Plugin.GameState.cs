using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using BepInEx;
using BepInEx.Logging;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using GreClient.Rules;
using GreClient.CardData;
using Wotc.Mtgo.Gre.External.Messaging;
using HarmonyLib;

namespace MtgaCoachBridge
{
    public partial class Plugin
    {
        private void HandleResolveGrpIds(PipeCommand cmd)
        {
            var gm = GetGameManager();
            var cardDb = gm != null ? gm.CardDatabase : null;
            if (cardDb == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "CardDatabase not available" });
                return;
            }

            var idsTok = cmd.Json["ids"] as JArray;
            if (idsTok == null || idsTok.Count == 0)
            {
                cmd.SetResponse(new JObject { ["ok"] = true, ["names"] = new JObject() });
                return;
            }

            MtgGameState gs = null;
            try { gs = gm.CurrentGameState; } catch { }

            var names = new JObject();
            foreach (var tok in idsTok)
            {
                uint gid;
                try { gid = tok.Value<uint>(); } catch { continue; }
                if (gid == 0) continue;

                string title = null;
                try
                {
                    title = cardDb.CardTitleProvider.GetCardTitle(gid, "en-US");
                }
                catch { }

                if (string.IsNullOrEmpty(title) && gs != null)
                {
                    try
                    {
                        var inst = FindInstanceByGrpId(gs, gid);
                        if (inst != null && inst.TitleId != 0)
                            title = cardDb.GreLocProvider.GetLocalizedText(inst.TitleId, null, false);
                    }
                    catch { }
                }

                if (!string.IsNullOrEmpty(title))
                    names[gid.ToString()] = title;
            }

            _log.LogInfo($"resolve_grp_ids: {idsTok.Count} requested, {names.Count} resolved");
            cmd.SetResponse(new JObject { ["ok"] = true, ["names"] = names });
        }

        private static MtgCardInstance FindInstanceByGrpId(MtgGameState gs, uint grpId)
        {
            var zones = new MtgZone[]
            {
                SafeZone(() => gs.Battlefield), SafeZone(() => gs.Stack),
                SafeZone(() => gs.LocalHand), SafeZone(() => gs.OpponentHand),
                SafeZone(() => gs.LocalGraveyard), SafeZone(() => gs.OpponentGraveyard),
                SafeZone(() => gs.Exile), SafeZone(() => gs.Command),
                SafeZone(() => gs.LocalLibrary), SafeZone(() => gs.OpponentLibrary),
            };
            foreach (var zone in zones)
            {
                if (zone?.VisibleCards == null) continue;
                foreach (var card in zone.VisibleCards)
                {
                    if (card != null && card.GrpId == grpId)
                        return card;
                }
            }
            return null;
        }

        private static MtgZone SafeZone(Func<MtgZone> getter)
        {
            try { return getter(); } catch { return null; }
        }

        // Phase 2: get_game_state â€” full game state from MtgGameState
        // -------------------------------------------------------------------

        private void HandleQueueBotMatch(PipeCommand cmd)
        {
            try
            {
                var home = UnityEngine.Object.FindObjectOfType<HomePageContentController>();
                if (home == null)
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "HomePageContentController not found" });
                    return;
                }

                Guid deckId = Guid.Empty;
                string deckIdStr = cmd.Json.Value<string>("deck_id");
                if (!string.IsNullOrEmpty(deckIdStr))
                {
                    Guid.TryParse(deckIdStr, out deckId);
                }

                var method = typeof(HomePageContentController).GetMethod(
                    "JoinMatchMaking",
                    BindingFlags.NonPublic | BindingFlags.Instance);

                if (method != null)
                {
                    method.Invoke(home, new object[] { "AIBotMatch", deckId });
                    cmd.SetResponse(new JObject { ["ok"] = true });
                }
                else
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "JoinMatchMaking method not found" });
                }
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
            }
        }

        private void HandleGetGameState(PipeCommand cmd)
        {
            var gm = GetGameManager();
            if (gm == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "GameManager not found" });
                return;
            }

            MtgGameState gs;
            try
            {
                gs = gm.CurrentGameState;
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"CurrentGameState error: {ex.Message}" });
                return;
            }

            if (gs == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No active game state" });
                return;
            }

            try
            {
                var resp = new JObject { ["ok"] = true };

                // Turn info
                resp["turn"] = new JObject
                {
                    ["turn_number"] = gs.GameWideTurn,
                    ["phase"] = gs.CurrentPhase.ToString(),
                    ["step"] = gs.CurrentStep.ToString(),
                    ["active_player"] = gs.ActivePlayer?.ControllerId ?? 0,
                    ["deciding_player"] = gs.DecidingPlayer?.ControllerId ?? 0,
                    ["stage"] = gs.Stage.ToString(),
                };

                // Players
                var playersArr = new JArray();
                if (gs.Players != null)
                {
                    foreach (var p in gs.Players)
                    {
                        var pObj = new JObject
                        {
                            ["seat_id"] = p.ControllerId,
                            ["life_total"] = p.LifeTotal,
                            ["is_local"] = p.IsLocalPlayer,
                            ["status"] = p.Status.ToString(),
                            ["mulligan_count"] = p.MulliganCount,
                            ["timeout_count"] = p.TimeoutCount,
                        };
                        // Mana pool
                        if (p.ManaPool != null && p.ManaPool.Count > 0)
                        {
                            var mana = new JObject();
                            foreach (var m in p.ManaPool)
                            {
                                string color = m.Color.ToString();
                                int current = mana[color]?.Value<int>() ?? 0;
                                mana[color] = current + (int)m.Count;
                            }
                            pObj["mana_pool"] = mana;
                        }
                        // Commander IDs
                        if (p.CommanderIds != null && p.CommanderIds.Count > 0)
                        {
                            var cmdIds = new JArray();
                            foreach (var cid in p.CommanderIds)
                                cmdIds.Add((int)cid);
                            pObj["commander_ids"] = cmdIds;
                        }
                        // Dungeon
                        // DungeonData is a struct â€” check via GrpId
                        try
                        {
                            var ds = p.DungeonState;
                            if (ds.DungeonGrpId != 0)
                            {
                                pObj["dungeon"] = new JObject
                                {
                                    ["dungeon_grp_id"] = (int)ds.DungeonGrpId,
                                    ["room_grp_id"] = (int)ds.CurrentRoomGrpId,
                                };
                            }
                        }
                        catch { }
                        // Designations (monarch, initiative, etc.)
                        if (p.Designations != null && p.Designations.Count > 0)
                        {
                            var desigs = new JArray();
                            foreach (var d in p.Designations)
                                desigs.Add(d.Type.ToString());
                            pObj["designations"] = desigs;
                        }
                        playersArr.Add(pObj);
                    }
                }
                resp["players"] = playersArr;

                // Zones with card instances
                resp["zones"] = SerializeZones(gs);

                // Combat info
                if (gs.AttackInfo != null && gs.AttackInfo.Count > 0)
                {
                    var attacks = new JObject();
                    foreach (var kvp in gs.AttackInfo)
                        attacks[kvp.Key.ToString()] = kvp.Value.TargetId.ToString();
                    resp["attack_info"] = attacks;
                }
                if (gs.BlockInfo != null && gs.BlockInfo.Count > 0)
                {
                    var blocks = new JObject();
                    foreach (var kvp in gs.BlockInfo)
                    {
                        var ids = new JArray();
                        try { foreach (var aid in kvp.Value.AttackerIds) ids.Add((int)aid); } catch { }
                        blocks[kvp.Key.ToString()] = ids;
                    }
                    resp["block_info"] = blocks;
                }

                // Designations (game-level)
                if (gs.Designations != null && gs.Designations.Count > 0)
                {
                    var desigs = new JArray();
                    foreach (var d in gs.Designations)
                    {
                        desigs.Add(new JObject
                        {
                            ["type"] = d.Type.ToString(),
                            ["affected_id"] = (int)d.AffectedId,
                        });
                    }
                    resp["designations"] = desigs;
                }

                // Timers
                if (gs.Timers != null && gs.Timers.Count > 0)
                {
                    resp["timers"] = SerializeTimers(gs.Timers);
                }

                // Pending interaction type
                var pending = FindPendingInteraction();
                if (pending != null)
                {
                    resp["pending_interaction"] = pending.GetType().Name;
                }

                cmd.SetResponse(resp);
            }
            catch (Exception ex)
            {
                _log.LogError($"get_game_state serialization error: {ex}");
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Serialization error: {ex.Message}" });
            }
        }

        private JObject SerializeZones(MtgGameState gs)
        {
            var zones = new JObject();

            void AddZone(string name, MtgZone zone)
            {
                if (zone == null) return;
                var cards = new JArray();
                if (zone.VisibleCards != null)
                {
                    foreach (var card in zone.VisibleCards)
                    {
                        cards.Add(SerializeCard(card));
                    }
                }
                zones[name] = new JObject
                {
                    ["zone_id"] = (int)zone.Id,
                    ["total_count"] = (int)zone.TotalCardCount,
                    ["cards"] = cards,
                };
            }

            try { AddZone("battlefield", gs.Battlefield); } catch { }
            try { AddZone("stack", gs.Stack); } catch { }
            try { AddZone("local_hand", gs.LocalHand); } catch { }
            try { AddZone("opponent_hand", gs.OpponentHand); } catch { }
            try { AddZone("local_graveyard", gs.LocalGraveyard); } catch { }
            try { AddZone("opponent_graveyard", gs.OpponentGraveyard); } catch { }
            try { AddZone("exile", gs.Exile); } catch { }
            try { AddZone("command", gs.Command); } catch { }
            try { AddZone("local_library", gs.LocalLibrary); } catch { }
            try { AddZone("opponent_library", gs.OpponentLibrary); } catch { }

            return zones;
        }

        private static JObject SerializeCard(MtgCardInstance card)
        {
            var obj = new JObject
            {
                ["instance_id"] = (int)card.InstanceId,
                ["grp_id"] = (int)card.GrpId,
                ["object_type"] = card.ObjectType.ToString(),
                ["is_tapped"] = card.IsTapped,
                ["owner_id"] = card.Owner?.ControllerId ?? 0,
                ["controller_id"] = card.Controller?.ControllerId ?? 0,
            };

            // Power/toughness
            try
            {
                if (card.Power.DefinedValue.HasValue)
                    obj["power"] = card.Power.Value;
                if (card.Toughness.DefinedValue.HasValue)
                    obj["toughness"] = card.Toughness.Value;
            }
            catch { }

            // Loyalty / Defense
            if (card.Loyalty.HasValue)
                obj["loyalty"] = (int)card.Loyalty.Value;
            if (card.Defense.HasValue)
                obj["defense"] = (int)card.Defense.Value;

            // Combat state
            if (card.IsAttacking)
            {
                obj["is_attacking"] = true;
                if (card.AttackTargetId != 0)
                    obj["attack_target_id"] = (int)card.AttackTargetId;
            }
            if (card.IsBlocking)
                obj["is_blocking"] = true;

            // Summoning sickness
            if (card.HasSummoningSickness)
                obj["summoning_sickness"] = true;

            // Phased out
            if (card.IsPhasedOut)
                obj["is_phased_out"] = true;

            // Damaged
            if (card.Damage > 0)
                obj["damage"] = (int)card.Damage;
            if (card.IsDamagedThisTurn)
                obj["damaged_this_turn"] = true;

            // Class level
            if (card.ClassLevel > 0)
                obj["class_level"] = card.ClassLevel;

            // Copy info
            if (card.IsCopy && card.CopyObjectGrpId != 0)
                obj["copied_from_grp_id"] = (int)card.CopyObjectGrpId;

            // Card types
            if (card.CardTypes != null && card.CardTypes.Count > 0)
            {
                var types = new JArray();
                foreach (var ct in card.CardTypes)
                    types.Add(ct.ToString());
                obj["card_types"] = types;
            }

            // Subtypes
            if (card.Subtypes != null && card.Subtypes.Count > 0)
            {
                var subs = new JArray();
                foreach (var st in card.Subtypes)
                    subs.Add(st.ToString());
                obj["subtypes"] = subs;
            }

            // Colors
            if (card.Colors != null && card.Colors.Count > 0)
            {
                var colors = new JArray();
                foreach (var c in card.Colors)
                    colors.Add(c.ToString());
                obj["colors"] = colors;
            }

            // Counters
            if (card.Counters != null && card.Counters.Count > 0)
            {
                var counters = new JObject();
                foreach (var kvp in card.Counters)
                    counters[kvp.Key.ToString()] = kvp.Value;
                obj["counters"] = counters;
            }

            // Color production (mana abilities)
            if (card.ColorProduction != null && card.ColorProduction.Count > 0)
            {
                var cp = new JArray();
                foreach (var c in card.ColorProduction)
                    cp.Add(c.ToString());
                obj["color_production"] = cp;
            }

            // Targets
            if (card.TargetIds != null && card.TargetIds.Count > 0)
            {
                var tids = new JArray();
                foreach (var tid in card.TargetIds)
                    tids.Add((int)tid);
                obj["target_ids"] = tids;
            }

            // Attached to
            if (card.AttachedToId != 0)
                obj["attached_to_id"] = (int)card.AttachedToId;

            // Attached with (auras/equipment on this card)
            if (card.AttachedWithIds != null && card.AttachedWithIds.Count > 0)
            {
                var awIds = new JArray();
                foreach (var aid in card.AttachedWithIds)
                    awIds.Add((int)aid);
                obj["attached_with_ids"] = awIds;
            }

            // Revealed to opponent
            if (card.RevealedToOpponent)
                obj["revealed_to_opponent"] = true;

            // Face down
            if (card.FaceDownState != null && card.FaceDownState.IsFaceDown)
                obj["face_down"] = true;

            // Crewed/saddled
            if (card.CrewedAndSaddledByIds != null && card.CrewedAndSaddledByIds.Count > 0)
                obj["crewed_this_turn"] = true;

            // Visibility
            obj["visibility"] = card.Visibility.ToString();

            return obj;
        }

        // -------------------------------------------------------------------
        // Phase 2: get_timer_state
        // -------------------------------------------------------------------

        private void HandleGetDraftState(PipeCommand cmd)
        {
            var draftController = FindObjectOfType<Wotc.Mtga.Wrapper.Draft.DraftContentController>();
            if (draftController == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "DraftContentController not found" });
                return;
            }

            var draftPod = draftController.DraftPod;
            if (draftPod == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No active draft pod" });
                return;
            }

            var resp = new JObject { ["ok"] = true };
            try
            {
                resp["draft_mode"] = draftPod.DraftMode.ToString();
                resp["pick_num_cards_to_take"] = draftPod.PickNumCardsToTake;

                var packCardsObj = draftPod.GetType().GetProperty("CardsInPack", BindingFlags.Public | BindingFlags.Instance);
                if (packCardsObj != null)
                {
                    var cardsList = packCardsObj.GetValue(draftPod) as System.Collections.IEnumerable;
                    if (cardsList != null)
                    {
                        var cardsArr = new JArray();
                        foreach (var cardGrpId in cardsList)
                        {
                            cardsArr.Add(Convert.ToInt32(cardGrpId));
                        }
                        resp["pack_cards"] = cardsArr;
                    }
                }

                var packNumberObj = draftPod.GetType().GetProperty("PackNumber", BindingFlags.Public | BindingFlags.Instance);
                if (packNumberObj != null) resp["pack_number"] = Convert.ToInt32(packNumberObj.GetValue(draftPod));
                
                var pickNumberObj = draftPod.GetType().GetProperty("PickNumber", BindingFlags.Public | BindingFlags.Instance);
                if (pickNumberObj != null) resp["pick_number"] = Convert.ToInt32(pickNumberObj.GetValue(draftPod));

                try
                {
                    var deckManagerField = draftController.GetType().GetField("_draftDeckManager", BindingFlags.NonPublic | BindingFlags.Instance);
                    if (deckManagerField != null)
                    {
                        var deckManager = deckManagerField.GetValue(draftController);
                        if (deckManager != null)
                        {
                            var getDeckMethod = deckManager.GetType().GetMethod("GetDeck", BindingFlags.Public | BindingFlags.Instance);
                            if (getDeckMethod != null)
                            {
                                var deck = getDeckMethod.Invoke(deckManager, null);
                                if (deck != null)
                                {
                                    var mainDeckIdsProp = deck.GetType().GetProperty("MainDeckIds", BindingFlags.Public | BindingFlags.Instance);
                                    var sideboardIdsProp = deck.GetType().GetProperty("SideboardIds", BindingFlags.Public | BindingFlags.Instance);
                                    var pickedArr = new JArray();
                                    
                                    if (mainDeckIdsProp != null) {
                                        var mainIds = mainDeckIdsProp.GetValue(deck) as System.Collections.IEnumerable;
                                        if (mainIds != null) foreach(var id in mainIds) pickedArr.Add(Convert.ToInt32(id));
                                    }
                                    if (sideboardIdsProp != null) {
                                        var sideboardIds = sideboardIdsProp.GetValue(deck) as System.Collections.IEnumerable;
                                        if (sideboardIds != null) foreach(var id in sideboardIds) pickedArr.Add(Convert.ToInt32(id));
                                    }
                                    
                                    resp["picked_cards"] = pickedArr;
                                }
                            }
                        }
                    }
                }
                catch(Exception ex)
                {
                    _log.LogWarning("Error getting drafted deck: " + ex.Message);
                }

                cmd.SetResponse(resp);
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Error reading draft state: {ex.Message}" });
            }
        }

        private void HandleGetTimerState(PipeCommand cmd)
        {
            var gm = GetGameManager();
            if (gm == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "GameManager not found" });
                return;
            }

            var gs = gm.CurrentGameState;
            if (gs == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No active game state" });
                return;
            }

            var resp = new JObject { ["ok"] = true };

            if (gs.Timers != null && gs.Timers.Count > 0)
            {
                resp["timers"] = SerializeTimers(gs.Timers);
            }

            // Also get per-player timers
            if (gs.Players != null)
            {
                var playerTimers = new JObject();
                foreach (var p in gs.Players)
                {
                    if (p.Timers != null && p.Timers.Count > 0)
                    {
                        var arr = new JArray();
                        foreach (var t in p.Timers)
                        {
                            arr.Add(new JObject
                            {
                                ["timer_id"] = (int)t.TimerId,
                                ["type"] = t.TimerType.ToString(),
                                ["duration_sec"] = (int)t.TotalDuration,
                                ["elapsed_sec"] = (int)t.ElapsedTime,
                                ["running"] = t.Running,
                                ["behavior"] = t.Behavior.ToString(),
                            });
                        }
                        playerTimers[p.ControllerId.ToString()] = arr;
                    }
                }
                if (playerTimers.Count > 0)
                    resp["player_timers"] = playerTimers;
            }

            cmd.SetResponse(resp);
        }

        private static JObject SerializeTimers(Dictionary<uint, MtgTimer> timers)
        {
            var result = new JObject();
            foreach (var kvp in timers)
            {
                var t = kvp.Value;
                result[kvp.Key.ToString()] = new JObject
                {
                    ["timer_id"] = (int)t.TimerId,
                    ["type"] = t.TimerType.ToString(),
                    ["duration_sec"] = (int)t.TotalDuration,
                    ["elapsed_sec"] = (int)t.ElapsedTime,
                    ["running"] = t.Running,
                    ["behavior"] = t.Behavior.ToString(),
                    ["warning_threshold"] = (int)t.WarningThreshold,
                };
            }
            return result;
        }

        // -------------------------------------------------------------------
        // Phase 2: get_match_info
        // -------------------------------------------------------------------

        private void HandleGetMatchInfo(PipeCommand cmd)
        {
            var gm = GetGameManager();
            if (gm == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "GameManager not found" });
                return;
            }

            var gs = gm.CurrentGameState;
            var resp = new JObject { ["ok"] = true };

            if (gs != null)
            {
                resp["game_state_id"] = gs.Id;
                resp["stage"] = gs.Stage.ToString();
                resp["turn"] = gs.GameWideTurn;
                resp["phase"] = gs.CurrentPhase.ToString();
                resp["step"] = gs.CurrentStep.ToString();

                if (gs.GameInfo != null)
                {
                    try
                    {
                        var gi = gs.GameInfo;
                        var info = new JObject();
                        // Use reflection to extract available fields
                        foreach (var prop in gi.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
                        {
                            try
                            {
                                var val = prop.GetValue(gi);
                                if (val != null)
                                    info[prop.Name] = val.ToString();
                            }
                            catch { }
                        }
                        resp["game_info"] = info;
                    }
                    catch { }
                }

                // Local/opponent info
                if (gs.LocalPlayer != null)
                {
                    resp["local_seat_id"] = gs.LocalPlayer.ControllerId;
                    resp["local_life"] = gs.LocalPlayer.LifeTotal;
                }
                if (gs.Opponent != null)
                {
                    resp["opponent_seat_id"] = gs.Opponent.ControllerId;
                    resp["opponent_life"] = gs.Opponent.LifeTotal;
                }
            }
            else
            {
                resp["stage"] = "no_game";
            }

            cmd.SetResponse(resp);
        }

        // -------------------------------------------------------------------
        // Enhanced action serialization (Phase 2)
        // -------------------------------------------------------------------

        private static JObject SerializeAction(Wotc.Mtgo.Gre.External.Messaging.Action action)
        {
            var obj = new JObject
            {
                ["actionType"] = action.ActionType.ToString(),
                ["grpId"] = (int)action.GrpId,
                ["instanceId"] = (int)action.InstanceId,
            };

            if (action.AbilityGrpId != 0)
                obj["abilityGrpId"] = (int)action.AbilityGrpId;
            if (action.SourceId != 0)
                obj["sourceId"] = (int)action.SourceId;
            if (action.AlternativeGrpId != 0)
                obj["alternativeGrpId"] = (int)action.AlternativeGrpId;
            if (action.FacetId != 0)
                obj["facetId"] = (int)action.FacetId;
            if (action.UniqueAbilityId != 0)
                obj["uniqueAbilityId"] = (int)action.UniqueAbilityId;

            // Castability flag from GRE
            obj["assumeCanBePaidFor"] = action.AssumeCanBePaidFor;

            // Mana cost
            if (action.ManaCost != null && action.ManaCost.Count > 0)
            {
                var costs = new JArray();
                for (int i = 0; i < action.ManaCost.Count; i++)
                {
                    var mc = action.ManaCost[i];
                    costs.Add(new JObject
                    {
                        ["color"] = mc.Color.ToString(),
                        ["count"] = (int)mc.Count
                    });
                }
                obj["manaCost"] = costs;
            }

            // Full AutoTap solution (Phase 2: serialize tap sequence, not just boolean)
            if (action.AutoTapSolution != null)
            {
                obj["hasAutoTap"] = true;
                try
                {
                    var ats = action.AutoTapSolution;
                    // AutoTapSolution has AutoTapActions â€” the lands to tap
                    var tapProp = ats.GetType().GetProperty("AutoTapActions")
                                  ?? ats.GetType().GetProperty("autoTapActions_");
                    if (tapProp != null)
                    {
                        var tapActions = tapProp.GetValue(ats) as System.Collections.IEnumerable;
                        if (tapActions != null)
                        {
                            var taps = new JArray();
                            foreach (var ta in tapActions)
                            {
                                var tapObj = new JObject();
                                // Extract instanceId and manaProduced via reflection
                                var instProp = ta.GetType().GetProperty("InstanceId");
                                var manaProp = ta.GetType().GetProperty("ManaId");
                                if (instProp != null)
                                    tapObj["instanceId"] = Convert.ToInt32(instProp.GetValue(ta));
                                if (manaProp != null)
                                    tapObj["manaId"] = Convert.ToInt32(manaProp.GetValue(ta));
                                taps.Add(tapObj);
                            }
                            if (taps.Count > 0)
                                obj["autoTapActions"] = taps;
                        }
                    }
                }
                catch (Exception ex)
                {
                    _log.LogDebug($"AutoTap serialization: {ex.Message}");
                }
            }

            // Targets on the action
            if (action.Targets != null && action.Targets.Count > 0)
            {
                var targets = new JArray();
                for (int i = 0; i < action.Targets.Count; i++)
                {
                    var t = action.Targets[i];
                    targets.Add(new JObject
                    {
                        ["targetId"] = (int)t.TargetIdx,
                    });
                }
                obj["targets"] = targets;
            }

            // Highlight (tells UI what to emphasize)
            if ((int)action.Highlight != 0)
                obj["highlight"] = action.Highlight.ToString();

            // ShouldStop flag
            if (action.ShouldStop)
                obj["shouldStop"] = true;

            // IsBatchable
            if (action.IsBatchable)
                obj["isBatchable"] = true;

            return obj;
        }

        // -------------------------------------------------------------------
        // Phase 3: Replay recording commands
        // -------------------------------------------------------------------

        private object _cachedReplayRecorder;
        private float _lastReplayRecorderLookup;

        /// <summary>
        /// Find the live TimedReplayRecorder instance.
        ///
        /// The recorder lives on the PAPA MonoBehaviour (the game's root singleton):
        ///   PAPA._instance  (private static)
        ///   PAPA.TimedReplayRecorder  (public property, type TimedReplayRecorder)
        ///
        /// TimedReplayRecorder is a plain C# class (NOT a MonoBehaviour), so
        /// FindObjectOfType will never find it.  We must go through PAPA.
        /// </summary>
        private object FindReplayRecorder()
        {
            float now = Time.unscaledTime;
            if (_cachedReplayRecorder != null && now - _lastReplayRecorderLookup < 5f)
                return _cachedReplayRecorder;
            _lastReplayRecorderLookup = now;
            _cachedReplayRecorder = null;

            try
            {
                var papa = FindPAPA();
                if (papa == null)
                {
                    _log.LogDebug("FindReplayRecorder: PAPA instance not found");
                    return null;
                }

                // PAPA.TimedReplayRecorder is a public auto-property
                var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
                var prop = papa.GetType().GetProperty("TimedReplayRecorder", flags);
                if (prop != null)
                {
                    var val = prop.GetValue(papa);
                    if (val != null)
                    {
                        _log.LogDebug($"Found TimedReplayRecorder via PAPA property ({val.GetType().FullName})");
                        _cachedReplayRecorder = val;
                        return val;
                    }
                    _log.LogDebug("PAPA.TimedReplayRecorder property exists but value is null");
                }
                else
                {
                    _log.LogDebug("PAPA type does not have TimedReplayRecorder property");
                }
            }
            catch (Exception ex)
            {
                _log.LogWarning($"FindReplayRecorder error: {ex.Message}");
            }
            return null;
        }

        /// <summary>
        /// Gets the PAPA singleton instance via its private static _instance field.
        /// PAPA is a MonoBehaviour so we can also fall back to FindObjectOfType.
        /// </summary>
        private object FindPAPA()
        {
            try
            {
                // Strategy 1: find the PAPA type and read its static _instance field
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type papaType = null;
                    try { papaType = asm.GetType("PAPA"); } catch { continue; }
                    if (papaType == null) continue;

                    var instanceField = papaType.GetField("_instance",
                        BindingFlags.NonPublic | BindingFlags.Static);
                    if (instanceField != null)
                    {
                        var inst = instanceField.GetValue(null);
                        if (inst != null) return inst;
                    }

                    // Fallback: PAPA is a MonoBehaviour, search the scene
                    var found = FindObjectOfType(papaType);
                    if (found != null) return found;
                }
            }
            catch (Exception ex)
            {
                _log.LogDebug($"FindPAPA error: {ex.Message}");
            }
            return null;
        }

        /// <summary>
        /// TimedReplayRecorder has no IsRecording property.
        /// Recording is active when the private _activeReplay (ReplayWriter) field is non-null.
        /// </summary>
        private bool IsRecorderRecording(object recorder)
        {
            if (recorder == null) return false;
            try
            {
                var field = recorder.GetType().GetField("_activeReplay",
                    BindingFlags.NonPublic | BindingFlags.Instance);
                if (field != null)
                    return field.GetValue(recorder) != null;
            }
            catch (Exception ex)
            {
                _log.LogDebug($"IsRecorderRecording error: {ex.Message}");
            }
            return false;
        }

        /// <summary>
        /// Gets the file path of the active replay being written.
        /// Path: TimedReplayRecorder._activeReplay (ReplayWriter) -> _writer (StreamWriter)
        ///       -> BaseStream (FileStream) -> Name
        /// </summary>
        private string GetRecorderFilePath(object recorder)
        {
            if (recorder == null) return null;
            try
            {
                var flags = BindingFlags.NonPublic | BindingFlags.Instance;
                var replayField = recorder.GetType().GetField("_activeReplay", flags);
                if (replayField == null) return null;
                var replayWriter = replayField.GetValue(recorder);
                if (replayWriter == null) return null;

                var writerField = replayWriter.GetType().GetField("_writer", flags);
                if (writerField == null) return null;
                var streamWriter = writerField.GetValue(replayWriter) as System.IO.StreamWriter;
                if (streamWriter == null) return null;

                var baseStream = streamWriter.BaseStream as System.IO.FileStream;
                if (baseStream != null)
                    return baseStream.Name;
            }
            catch (Exception ex)
            {
                _log.LogDebug($"GetRecorderFilePath error: {ex.Message}");
            }
            return null;
        }

        /// <summary>
        /// Sets the SaveDSReplay preference using CachedPlayerPrefs (via reflection)
        /// so that the in-memory cache stays in sync.  Falls back to raw PlayerPrefs.
        /// MDNPlayerPrefs.SaveDSReplays reads from CachedPlayerPrefs, so writing
        /// directly to PlayerPrefs would leave the cache stale.
        /// </summary>

    }
}
