using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
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

namespace MtgaCoachBridge
{
    [BepInPlugin(PluginInfo.GUID, PluginInfo.Name, PluginInfo.Version)]
    public class Plugin : BaseUnityPlugin
    {
        private static ManualLogSource _log;
        private Thread _pipeThread;
        private volatile bool _running;
        private readonly ConcurrentQueue<PipeCommand> _pendingCommands = new ConcurrentQueue<PipeCommand>();

        private BaseUserRequest _lastKnownRequest;
        private readonly object _interactionLock = new object();

        // Cached reference to GameManager (only valid on main thread)
        private GameManager _cachedGameManager;
        private float _lastGameManagerLookup;

        private void Awake()
        {
            _log = Logger;
            _log.LogInfo($"MtgaCoachBridge v{PluginInfo.Version} loaded");
            DontDestroyOnLoad(gameObject);

            _running = true;
            _pipeThread = new Thread(PipeClientLoop)
            {
                IsBackground = true,
                Name = "MtgaCoachBridge-Pipe"
            };
            _pipeThread.Start();
        }

        private void OnDestroy()
        {
            _log.LogInfo("OnDestroy called — pipe thread continues (IsBackground)");
        }

        private void Update()
        {
            while (_pendingCommands.TryDequeue(out var cmd))
            {
                try
                {
                    ProcessCommand(cmd);
                }
                catch (Exception ex)
                {
                    _log.LogError($"Error processing command: {ex}");
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = false,
                        ["error"] = ex.Message
                    });
                }
            }
        }

        // -------------------------------------------------------------------
        // Named pipe CLIENT — connects to Python-owned server pipe.
        // Reversed architecture: Python creates the pipe, plugin connects.
        // This avoids MTGA internals grabbing the pipe and scene transitions
        // killing the server.
        // -------------------------------------------------------------------

        private void PipeClientLoop()
        {
            while (true)
            {
                NamedPipeClientStream pipe = null;
                try
                {
                    pipe = new NamedPipeClientStream(
                        ".",
                        "mtgacoach_bridge_v2",
                        PipeDirection.InOut
                    );

                    _log.LogInfo("Pipe client connecting to \\\\.\\pipe\\mtgacoach_bridge_v2...");
                    pipe.Connect(2000); // 2s timeout — retry if Python isn't up yet
                    _log.LogInfo("Pipe client connected to Python server");

                    HandleClient(pipe);
                    _log.LogInfo("HandleClient returned, will reconnect");
                }
                catch (TimeoutException)
                {
                    // Python server not ready yet — retry
                }
                catch (Exception ex)
                {
                    _log.LogWarning($"Pipe client error: {ex.GetType().Name}: {ex.Message}");
                }
                finally
                {
                    try { pipe?.Dispose(); } catch { }
                }

                Thread.Sleep(1000); // Wait before reconnect attempt
            }
        }

        private void HandleClient(PipeStream pipe)
        {
            using var reader = new StreamReader(pipe, Encoding.UTF8, false, 4096, leaveOpen: true);
            using var writer = new StreamWriter(pipe, Encoding.UTF8, 4096, leaveOpen: true)
            {
                AutoFlush = true
            };

            while (pipe.IsConnected)
            {
                string line;
                try
                {
                    line = reader.ReadLine();
                }
                catch
                {
                    break;
                }

                if (line == null)
                    break;

                line = line.Trim();
                if (string.IsNullOrEmpty(line))
                    continue;

                try
                {
                    var json = JObject.Parse(line);
                    string action = json.Value<string>("action") ?? "";

                    // Handle ping directly on pipe thread — doesn't need Unity main thread.
                    // This is critical because Update() stops after OnDestroy (scene transitions).
                    if (action == "ping")
                    {
                        var pingResp = new JObject
                        {
                            ["ok"] = true,
                            ["version"] = PluginInfo.Version
                        };
                        writer.WriteLine(pingResp.ToString(Formatting.None));
                        continue;
                    }

                    // All other commands need Unity main thread (GameManager access)
                    var cmd = new PipeCommand(json);
                    _pendingCommands.Enqueue(cmd);

                    var response = cmd.WaitForResponse(5000);
                    writer.WriteLine(response.ToString(Formatting.None));
                }
                catch (Exception ex)
                {
                    var errorResp = new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Parse error: {ex.Message}"
                    };
                    try { writer.WriteLine(errorResp.ToString(Formatting.None)); } catch { break; }
                }
            }

            _log.LogInfo("Pipe client disconnected");
            writer.Dispose();
        }

        // -------------------------------------------------------------------
        // GameManager access (cached, main thread only)
        // -------------------------------------------------------------------

        private GameManager GetGameManager()
        {
            float now = Time.unscaledTime;
            if (_cachedGameManager == null || now - _lastGameManagerLookup > 5f)
            {
                _cachedGameManager = FindObjectOfType<GameManager>();
                _lastGameManagerLookup = now;
            }
            return _cachedGameManager;
        }

        // -------------------------------------------------------------------
        // Command processing (runs on Unity main thread)
        // -------------------------------------------------------------------

        private void ProcessCommand(PipeCommand cmd)
        {
            string action = cmd.Json.Value<string>("action") ?? "";

            switch (action)
            {
                case "ping":
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = true,
                        ["version"] = PluginInfo.Version
                    });
                    break;

                case "get_pending_actions":
                    HandleGetPendingActions(cmd);
                    break;

                case "submit_action":
                    HandleSubmitAction(cmd);
                    break;

                case "submit_pass":
                    HandleSubmitPass(cmd);
                    break;

                case "get_game_state":
                    HandleGetGameState(cmd);
                    break;

                case "get_timer_state":
                    HandleGetTimerState(cmd);
                    break;

                case "get_match_info":
                    HandleGetMatchInfo(cmd);
                    break;

                case "enable_replay":
                    HandleEnableReplay(cmd);
                    break;

                case "disable_replay":
                    HandleDisableReplay(cmd);
                    break;

                case "get_replay_status":
                    HandleGetReplayStatus(cmd);
                    break;

                case "list_replays":
                    HandleListReplays(cmd);
                    break;

                default:
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Unknown action: {action}"
                    });
                    break;
            }
        }

        // -------------------------------------------------------------------
        // Find pending interaction
        // -------------------------------------------------------------------

        private BaseUserRequest FindPendingInteraction()
        {
            try
            {
                var gameManager = GetGameManager();
                if (gameManager == null)
                {
                    _log.LogDebug("GameManager not found in scene");
                    return null;
                }

                var wfc = gameManager.WorkflowController;
                if (wfc == null)
                {
                    _log.LogDebug("WorkflowController is null");
                    return null;
                }

                object workflow = wfc.CurrentWorkflow;
                if (workflow == null)
                {
                    try
                    {
                        var pendingProp = wfc.GetType().GetProperty("PendingWorkflow",
                            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                        if (pendingProp != null)
                            workflow = pendingProp.GetValue(wfc);
                    }
                    catch { }
                }

                if (workflow == null)
                {
                    _log.LogDebug("No current or pending workflow");
                    return null;
                }

                var reqProp = workflow.GetType().GetProperty("BaseRequest",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (reqProp == null)
                {
                    reqProp = workflow.GetType().GetProperty("Request",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                }
                if (reqProp != null)
                {
                    var request = reqProp.GetValue(workflow) as BaseUserRequest;
                    if (request != null)
                    {
                        _log.LogDebug($"Found pending request: {request.GetType().Name}");
                        return request;
                    }
                }

                // Fallback: MatchManager reflection
                try
                {
                    var mm = gameManager.MatchManager;
                    if (mm != null)
                    {
                        var field = mm.GetType().GetField("_pendingInteraction",
                            BindingFlags.NonPublic | BindingFlags.Instance);
                        if (field != null)
                        {
                            var request = field.GetValue(mm) as BaseUserRequest;
                            if (request != null)
                                return request;
                        }
                    }
                }
                catch (Exception ex)
                {
                    _log.LogDebug($"MatchManager fallback: {ex.Message}");
                }
            }
            catch (Exception ex)
            {
                _log.LogDebug($"FindPendingInteraction error: {ex.Message}");
            }

            return null;
        }

        // -------------------------------------------------------------------
        // Existing commands: get_pending_actions, submit_action, submit_pass
        // -------------------------------------------------------------------

        private void HandleGetPendingActions(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request == null)
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["has_pending"] = false,
                    ["request_type"] = JValue.CreateNull()
                });
                return;
            }

            lock (_interactionLock)
            {
                _lastKnownRequest = request;
            }

            var resp = new JObject
            {
                ["ok"] = true,
                ["has_pending"] = true,
                ["request_type"] = request.Type.ToString(),
                ["can_cancel"] = request.CanCancel,
                ["allow_undo"] = request.AllowUndo
            };

            if (request is ActionsAvailableRequest actionsReq)
            {
                var actionsArr = new JArray();
                for (int i = 0; i < actionsReq.Actions.Count; i++)
                {
                    actionsArr.Add(SerializeAction(actionsReq.Actions[i]));
                }
                resp["actions"] = actionsArr;
                resp["can_pass"] = actionsReq.CanPass;
            }

            cmd.SetResponse(resp);
        }

        private void HandleSubmitAction(PipeCommand cmd)
        {
            BaseUserRequest request;
            lock (_interactionLock)
            {
                request = _lastKnownRequest;
            }

            if (request == null)
                request = FindPendingInteraction();

            if (request == null)
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = "No pending interaction"
                });
                return;
            }

            if (request is ActionsAvailableRequest actionsReq)
            {
                int actionIndex = cmd.Json.Value<int>("action_index");
                bool autoPass = cmd.Json.Value<bool>("auto_pass");

                if (actionIndex < 0 || actionIndex >= actionsReq.Actions.Count)
                {
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Action index {actionIndex} out of range (0-{actionsReq.Actions.Count - 1})"
                    });
                    return;
                }

                var action = actionsReq.Actions[actionIndex];
                _log.LogInfo($"Submitting action [{actionIndex}]: {action.ActionType} grpId={action.GrpId} instanceId={action.InstanceId}");

                actionsReq.SubmitAction(action, autoPass);

                lock (_interactionLock)
                {
                    _lastKnownRequest = null;
                }

                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["submitted_type"] = action.ActionType.ToString(),
                    ["submitted_grp_id"] = (int)action.GrpId,
                    ["submitted_instance_id"] = (int)action.InstanceId
                });
            }
            else
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = $"Pending request is {request.GetType().Name}, not ActionsAvailableRequest"
                });
            }
        }

        private void HandleSubmitPass(PipeCommand cmd)
        {
            BaseUserRequest request;
            lock (_interactionLock)
            {
                request = _lastKnownRequest;
            }

            if (request == null)
                request = FindPendingInteraction();

            if (request == null)
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = "No pending interaction"
                });
                return;
            }

            if (request is ActionsAvailableRequest actionsReq && actionsReq.CanPass)
            {
                _log.LogInfo("Submitting pass");
                actionsReq.SubmitPass();

                lock (_interactionLock)
                {
                    _lastKnownRequest = null;
                }

                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["submitted_type"] = "Pass"
                });
            }
            else
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = "Cannot pass on current interaction"
                });
            }
        }

        // -------------------------------------------------------------------
        // Phase 2: get_game_state — full game state from MtgGameState
        // -------------------------------------------------------------------

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
                        // DungeonData is a struct — check via GrpId
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
                    // AutoTapSolution has AutoTapActions — the lands to tap
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

        private void HandleEnableReplay(PipeCommand cmd)
        {
            try
            {
                // Set the PlayerPrefs flag that TimedReplayRecorder checks
                PlayerPrefs.SetInt("SaveDSReplay", 1);
                PlayerPrefs.Save();

                // Set replay name prefix if provided
                string prefix = cmd.Json.Value<string>("replay_name");
                if (!string.IsNullOrEmpty(prefix))
                {
                    PlayerPrefs.SetString("ReplayName", prefix);
                    PlayerPrefs.Save();
                }

                _log.LogInfo($"Replay recording enabled (prefix: {prefix ?? "default"})");
                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["enabled"] = true,
                    ["replay_folder"] = GetReplayFolder(),
                });
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
            }
        }

        private void HandleDisableReplay(PipeCommand cmd)
        {
            try
            {
                PlayerPrefs.SetInt("SaveDSReplay", 0);
                PlayerPrefs.Save();
                _log.LogInfo("Replay recording disabled");
                cmd.SetResponse(new JObject { ["ok"] = true, ["enabled"] = false });
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
            }
        }

        private void HandleGetReplayStatus(PipeCommand cmd)
        {
            try
            {
                bool enabled = PlayerPrefs.GetInt("SaveDSReplay", 0) == 1;
                string replayName = PlayerPrefs.GetString("ReplayName", "");
                string folder = GetReplayFolder();

                var resp = new JObject
                {
                    ["ok"] = true,
                    ["recording_enabled"] = enabled,
                    ["replay_name"] = replayName,
                    ["replay_folder"] = folder,
                };

                // Count existing replays
                try
                {
                    if (System.IO.Directory.Exists(folder))
                    {
                        var files = System.IO.Directory.GetFiles(folder, "*.rply");
                        resp["replay_count"] = files.Length;
                        if (files.Length > 0)
                        {
                            // Most recent replay
                            Array.Sort(files);
                            resp["latest_replay"] = System.IO.Path.GetFileName(files[files.Length - 1]);
                        }
                    }
                }
                catch { }

                cmd.SetResponse(resp);
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
            }
        }

        private void HandleListReplays(PipeCommand cmd)
        {
            try
            {
                string folder = GetReplayFolder();
                var resp = new JObject
                {
                    ["ok"] = true,
                    ["replay_folder"] = folder,
                };

                if (System.IO.Directory.Exists(folder))
                {
                    var files = System.IO.Directory.GetFiles(folder, "*.rply");
                    Array.Sort(files);
                    var replays = new JArray();
                    // Return most recent first, limit to 50
                    int start = Math.Max(0, files.Length - 50);
                    for (int i = files.Length - 1; i >= start; i--)
                    {
                        var fi = new System.IO.FileInfo(files[i]);
                        replays.Add(new JObject
                        {
                            ["filename"] = fi.Name,
                            ["path"] = fi.FullName,
                            ["size_bytes"] = fi.Length,
                            ["created"] = fi.CreationTime.ToString("o"),
                            ["modified"] = fi.LastWriteTime.ToString("o"),
                        });
                    }
                    resp["replays"] = replays;
                    resp["total_count"] = files.Length;
                }
                else
                {
                    resp["replays"] = new JArray();
                    resp["total_count"] = 0;
                }

                cmd.SetResponse(resp);
            }
            catch (Exception ex)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
            }
        }

        private static string GetReplayFolder()
        {
            // Desktop: Application.streamingAssetsPath + "/Tests"
            // This matches ReplayUtilities.GetReplayFolder()
            string folder = System.IO.Path.Combine(Application.streamingAssetsPath, "Tests");
            if (!System.IO.Directory.Exists(folder))
            {
                // Fallback: persistent data path
                folder = System.IO.Path.Combine(Application.persistentDataPath, "Replays");
            }
            return folder;
        }
    }

    // -------------------------------------------------------------------
    // Helper: pipe command with synchronous response channel
    // -------------------------------------------------------------------

    internal class PipeCommand
    {
        public JObject Json { get; }
        private JObject _response;
        private readonly ManualResetEventSlim _signal = new ManualResetEventSlim(false);

        public PipeCommand(JObject json)
        {
            Json = json;
        }

        public void SetResponse(JObject response)
        {
            _response = response;
            _signal.Set();
        }

        public JObject WaitForResponse(int timeoutMs)
        {
            if (_signal.Wait(timeoutMs))
                return _response;

            return new JObject
            {
                ["ok"] = false,
                ["error"] = "Command timed out waiting for main thread"
            };
        }
    }

    internal static class PluginInfo
    {
        public const string GUID = "com.mtgacoach.grebridge";
        public const string Name = "MtgaCoach GRE Bridge";
        public const string Version = "0.3.0";
    }
}
