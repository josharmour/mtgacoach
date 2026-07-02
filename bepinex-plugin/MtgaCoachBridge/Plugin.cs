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
    [BepInPlugin(PluginInfo.GUID, PluginInfo.Name, PluginInfo.Version)]
    public class Plugin : BaseUnityPlugin
    {
        public static Plugin Instance { get; private set; }

        private const int UnityCommandTimeoutMs = 15000;
        internal static ManualLogSource _log;
        private Thread _pipeThread;
        private volatile bool _running;

        private BaseUserRequest _lastKnownRequest;
        private readonly object _interactionLock = new object();

        private sealed class CastingTimeOptionEntry
        {
            public JObject Payload { get; }
            private readonly System.Action _submit;

            public CastingTimeOptionEntry(JObject payload, System.Action submit)
            {
                Payload = payload;
                _submit = submit;
            }

            public void Submit()
            {
                _submit();
            }
        }

        // Cap on enumerated entries for variable-X CastingTimeOption requests
        // (NumericInput / Replicate). Most spells with X have a small range,
        // and the bridge protocol is per-entry-index — too many entries make
        // the planner choice noisy.
        private const int MaxNumericInputEntries = 20;

        private static IEnumerable<uint> EnumerateNumericInputValues(CastingTimeOption_NumericInputRequest req)
        {
            var disallowed = req.DisallowedValues != null ? new HashSet<uint>(req.DisallowedValues) : new HashSet<uint>();
            uint step = req.StepSize > 0 ? req.StepSize : 1;
            int yielded = 0;
            for (uint v = req.Min; v <= req.Max && yielded < MaxNumericInputEntries; v += step)
            {
                if (disallowed.Contains(v)) continue;
                if (req.DisallowEven && v % 2 == 0) continue;
                if (req.DisallowOdd && v % 2 == 1) continue;
                yield return v;
                yielded++;
            }
        }

        private void Awake()
        {
            Instance = this;
            _log = Logger;
            _log.LogInfo($"MtgaCoachBridge v{PluginInfo.Version} loaded");
            DontDestroyOnLoad(gameObject);

            try
            {
                var harmony = new Harmony(PluginInfo.GUID);
                harmony.PatchAll();
                _log.LogInfo("Harmony patches applied successfully.");
            }
            catch (Exception ex)
            {
                _log.LogError($"Failed to apply Harmony patches: {ex}");
            }

            // Unity-thread state (sync context, command queue, GameManager
            // cache) lives on a separate persistent host. BepInEx's manager
            // GameObject — which owns this Plugin — gets destroyed on the
            // first MTGA scene transition. The host survives because it is
            // a root-level GameObject we own with HideAndDontSave + DDOL.
            MtgaCoachHost.CreateOrFind(_log, ExecutePipeCommand);

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
            _log?.LogInfo("Plugin OnDestroy — pipe thread + persistent host continue");
        }

        internal void ExecutePipeCommand(PipeCommand cmd)
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

        internal void SetLastKnownRequest(BaseUserRequest request)
        {
            lock (_interactionLock)
            {
                _lastKnownRequest = request;
            }
        }

        private JObject DispatchCommandToUnityThread(PipeCommand cmd, int timeoutMs)
        {
            var host = MtgaCoachHost.Instance;
            if (host == null)
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = "MtgaCoachHost not available"
                });
                return cmd.WaitForResponse(timeoutMs);
            }

            if (Thread.CurrentThread.ManagedThreadId == host.MainThreadId)
            {
                ExecutePipeCommand(cmd);
                return cmd.WaitForResponse(timeoutMs);
            }

            var unityContext = host.UnityContext;
            if (unityContext != null)
            {
                // Primary path: Post via captured Unity SyncContext.
                // Fallback queue is processed by the host's Update() if Post
                // ever fails to deliver.
                unityContext.Post(_ => ExecutePipeCommand(cmd), null);
            }
            else
            {
                host.PendingCommands.Enqueue(cmd);
            }

            return cmd.WaitForResponse(timeoutMs);
        }

        // -------------------------------------------------------------------
        // Named pipe CLIENT — connects to Python-owned server pipe.
        // Reversed architecture: Python creates the pipe, plugin connects.
        // This avoids MTGA internals grabbing the pipe and scene transitions
        // killing the server.
        // -------------------------------------------------------------------

        private void PipeClientLoop()
        {
            // Reconnect loop with adaptive backoff.
            //   - Short connect timeout (1s) so we poll frequently when the
            //     Python server is momentarily down (scene transitions,
            //     Python process restart).
            //   - Successful connections reset the retry delay.
            //   - Failed reconnects back off from 200ms up to 2s so we don't
            //     spin on the CPU when Python is truly gone.
            int retryMs = 200;
            const int minRetryMs = 200;
            const int maxRetryMs = 2000;
            int consecutiveTimeouts = 0;

            while (true)
            {
                TcpClient client = null;
                bool connectedThisIteration = false;
                try
                {
                    client = new TcpClient();
                    var result = client.BeginConnect("127.0.0.1", 44222, null, null);
                    bool success = result.AsyncWaitHandle.WaitOne(1000); // 1s timeout — retry if Python isn't up yet
                    if (!success)
                    {
                        throw new TimeoutException("TCP connection to Python server timed out");
                    }
                    client.EndConnect(result);

                    // Guard against TCP self-connect: on loopback, if the OS
                    // hands our client socket source port 44222 (same as the
                    // dest), a TCP simultaneous-open connects us to ourselves.
                    // The socket reports ESTABLISHED but there is no Python
                    // server on the other end, so we'd hang forever. Detect it
                    // (local port == remote port) and retry with a fresh socket.
                    var localEp = client.Client.LocalEndPoint as System.Net.IPEndPoint;
                    if (localEp != null && localEp.Port == 44222)
                    {
                        _log.LogWarning("TCP self-connect detected (local port == 44222); retrying");
                        try { client.Close(); } catch { }
                        System.Threading.Thread.Sleep(50);
                        continue;
                    }

                    connectedThisIteration = true;
                    consecutiveTimeouts = 0;
                    retryMs = minRetryMs;
                    _log.LogInfo("TCP client connected to Python server on port 44222");

                    HandleClient(client);
                    _log.LogInfo("TCP client lost connection (HandleClient returned), reconnecting...");
                }
                catch (TimeoutException)
                {
                    // Python server not up yet — usually means the Python
                    // process is restarting or between server recreations.
                    consecutiveTimeouts++;
                    if (consecutiveTimeouts == 1 || consecutiveTimeouts % 10 == 0)
                    {
                        _log.LogInfo(
                            $"TCP client: Python server not available " +
                            $"(timeout {consecutiveTimeouts}), retrying in {retryMs}ms"
                        );
                    }
                    // Back off on repeated timeouts so we don't spin.
                    retryMs = System.Math.Min(maxRetryMs, retryMs * 2);
                }
                catch (System.Net.Sockets.SocketException sex)
                {
                    // Connection refused = Python server not running. Same
                    // situation as a connect timeout, so same treatment:
                    // back off and log every 10th attempt. (Previously this
                    // reset the retry to 200ms and logged EVERY attempt —
                    // ~5 log lines/sec for as long as Python was down.)
                    consecutiveTimeouts++;
                    if (consecutiveTimeouts == 1 || consecutiveTimeouts % 10 == 0)
                    {
                        _log.LogInfo(
                            $"TCP client: Python server not available " +
                            $"({sex.SocketErrorCode}, attempt {consecutiveTimeouts}), " +
                            $"retrying in {retryMs}ms"
                        );
                    }
                    retryMs = System.Math.Min(maxRetryMs, retryMs * 2);
                }
                catch (Exception ex)
                {
                    _log.LogWarning($"TCP client error: {ex.GetType().Name}: {ex.Message}");
                    // Reset retry after non-timeout errors — they're usually
                    // transient.
                    retryMs = minRetryMs;
                }
                finally
                {
                    try { client?.Close(); } catch { }
                }

                // If we DID connect successfully and HandleClient returned,
                // reconnect aggressively — the Python server should be ready
                // to accept us again.
                int sleepMs = connectedThisIteration ? minRetryMs : retryMs;
                Thread.Sleep(sleepMs);
            }
        }

        private void HandleClient(TcpClient client)
        {
            using var stream = client.GetStream();
            using var reader = new StreamReader(stream, Encoding.UTF8, false, 4096, leaveOpen: true);
            using var writer = new StreamWriter(stream, new UTF8Encoding(false), 4096, leaveOpen: true)
            {
                AutoFlush = true
            };

            while (client.Connected)
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

                    // Handle ping directly on TCP thread — doesn't need Unity main thread.
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
                    var response = DispatchCommandToUnityThread(cmd, UnityCommandTimeoutMs);
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

            _log.LogInfo("TCP client disconnected");
            writer.Dispose();
        }

        // -------------------------------------------------------------------
        // GameManager access (cached, main thread only)
        // -------------------------------------------------------------------

        private GameManager GetGameManager()
        {
            // Host's Update() refreshes this every second. The host lives on
            // its own DDOL'd GameObject so it survives MTGA scene transitions
            // even when this Plugin's MonoBehaviour is destroyed.
            var host = MtgaCoachHost.Instance;
            if (host != null)
            {
                var cached = host.GetGameManager();
                if (cached != null)
                    return cached;
            }
            // Fallback: direct lookup (e.g. before host's first Update tick).
            return FindObjectOfType<GameManager>();
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

                case "submit_blockers":
                    HandleSubmitBlockers(cmd);
                    break;

                case "submit_attackers":
                    HandleSubmitAttackers(cmd);
                    break;

                case "submit_mulligan":
                    HandleSubmitMulligan(cmd);
                    break;

                case "submit_choose_starting_player":
                    HandleSubmitChooseStartingPlayer(cmd);
                    break;

                case "submit_selection":
                    HandleSubmitSelection(cmd);
                    break;

                case "submit_group":
                    HandleSubmitGroup(cmd);
                    break;

                case "submit_optional":
                    HandleSubmitOptional(cmd);
                    break;

                case "submit_numeric":
                    HandleSubmitNumeric(cmd);
                    break;

                case "submit_targets":
                    HandleSubmitTargets(cmd);
                    break;

                case "submit_assign_damage":
                    HandleSubmitAssignDamage(cmd);
                    break;

                case "submit_distribution":
                    HandleSubmitDistribution(cmd);
                    break;

                case "submit_order":
                    HandleSubmitOrder(cmd);
                    break;

                case "submit_select_replacement":
                    HandleSubmitSelectReplacement(cmd);
                    break;

                case "submit_select_counters":
                    HandleSubmitSelectCounters(cmd);
                    break;

                case "submit_string_input":
                    HandleSubmitStringInput(cmd);
                    break;

                case "submit_intermission":
                    HandleSubmitIntermission(cmd);
                    break;

                case "submit_gather":
                    HandleSubmitGather(cmd);
                    break;

                case "submit_auto_tap":
                    HandleSubmitAutoTap(cmd);
                    break;

                case "submit_select_from_groups":
                    HandleSubmitSelectFromGroups(cmd);
                    break;

                case "submit_select_n_group":
                    HandleSubmitSelectNGroup(cmd);
                    break;

                case "submit_search_from_groups":
                    HandleSubmitSearchFromGroups(cmd);
                    break;

                case "submit_casting_mana_type":
                    HandleSubmitCastingManaType(cmd);
                    break;

                case "auto_respond":
                    HandleAutoRespond(cmd);
                    break;

                case "cancel_action":
                    HandleCancelAction(cmd);
                    break;

                case "get_game_state":
                    HandleGetGameState(cmd);
                    break;

                case "resolve_grp_ids":
                    HandleResolveGrpIds(cmd);
                    break;

                case "get_draft_state":
                    HandleGetDraftState(cmd);
                    break;

                case "get_timer_state":
                    HandleGetTimerState(cmd);
                    break;

                case "get_match_info":
                    HandleGetMatchInfo(cmd);
                    break;

                case "get_card_positions":
                    HandleGetCardPositions(cmd);
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

                case "start_bot_battle":
                    cmd.SetResponse(BotBattleBridge.Start(cmd.Json, _log));
                    break;

                case "return_to_home":
                    cmd.SetResponse(PracticeMatchBridge.ReturnToHome(_log));
                    break;

                case "start_practice_match":
                    cmd.SetResponse(PracticeMatchBridge.Start(cmd.Json, _log));
                    break;

                case "bot_battle_status":
                    cmd.SetResponse(BotBattleBridge.GetStatus());
                    break;

                case "list_scenes":
                    {
                        var sceneList = new JArray();
                        for (int i = 0; i < UnityEngine.SceneManagement.SceneManager.sceneCountInBuildSettings; i++)
                        {
                            sceneList.Add(UnityEngine.SceneManagement.SceneUtility.GetScenePathByBuildIndex(i));
                        }
                        cmd.SetResponse(new JObject { ["ok"] = true, ["scenes"] = sceneList });
                    }
                    break;

                case "inspect_class":
                    {
                        string typeName = cmd.Json.Value<string>("type") ?? "BotBattleScene";
                        try
                        {
                            var type = Type.GetType(typeName);
                            if (type == null)
                            {
                                // Try in the assembly of BotBattleScene
                                type = typeof(BotBattleScene).Assembly.GetType(typeName);
                            }
                            if (type == null)
                            {
                                // Try in assembly of Plugin
                                type = typeof(Plugin).Assembly.GetType(typeName);
                            }
                            if (type == null)
                            {
                                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Type {typeName} not found" });
                                break;
                            }
                            var methods = new JArray();
                            foreach (var m in type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static))
                            {
                                string prefix = "";
                                if (m.IsStatic) prefix += "static ";
                                if (m.IsPublic) prefix += "public ";
                                else if (m.IsPrivate) prefix += "private ";
                                methods.Add($"{prefix}{m.ReturnType.Name} {m.Name}({string.Join(", ", Array.ConvertAll(m.GetParameters(), p => $"{p.ParameterType.Name} {p.Name}"))})");
                            }
                            var fields = new JArray();
                            foreach (var f in type.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static))
                            {
                                string prefix = "";
                                if (f.IsStatic) prefix += "static ";
                                if (f.IsLiteral) prefix += "const ";
                                if (f.IsPublic) prefix += "public ";
                                else if (f.IsPrivate) prefix += "private ";
                                fields.Add($"{prefix}{f.FieldType.Name} {f.Name}");
                            }
                            cmd.SetResponse(new JObject
                            {
                                ["ok"] = true,
                                ["name"] = type.FullName,
                                ["base"] = type.BaseType?.FullName,
                                ["methods"] = methods,
                                ["fields"] = fields
                            });
                        }
                        catch (Exception ex)
                        {
                            cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = ex.Message });
                        }
                    }
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
                ["request_class"] = request.GetType().Name,
                ["can_cancel"] = request.CanCancel,
                ["allow_undo"] = request.AllowUndo
            };
            // Request identity for the Python-side FSM: static-option
            // windows (Mulligan keep/mull) are content-identical across
            // rounds, so without these ids round 2 looks like a re-present
            // of round 1 (false REJECTED; see fable-improvements Phase E).
            try
            {
                resp["game_state_id"] = (long)request.OriginalMessage.GameStateId;
                resp["msg_id"] = (long)request.OriginalMessage.MsgId;
            }
            catch { }

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
            else if (request is DeclareBlockersRequest blockersReq)
            {
                var blockersArr = new JArray();
                foreach (var b in blockersReq.AllBlockers)
                {
                    var bo = new JObject
                    {
                        ["blockerInstanceId"] = (int)b.BlockerInstanceId,
                        ["mustBlock"] = b.MustBlock,
                        ["minAttackers"] = (int)b.MinAttackers,
                        ["maxAttackers"] = (int)b.MaxAttackers
                    };
                    var attackerIds = new JArray();
                    foreach (var aid in b.AttackerInstanceIds) attackerIds.Add((int)aid);
                    bo["attackerInstanceIds"] = attackerIds;
                    blockersArr.Add(bo);
                }
                resp["blockers"] = blockersArr;
                resp["can_pass"] = false;
            }
            else if (request is DeclareAttackerRequest attackerReq)
            {
                var attackersArr = new JArray();
                foreach (var a in attackerReq.QualifiedAttackers)
                {
                    var ao = new JObject
                    {
                        ["attackerInstanceId"] = (int)a.AttackerInstanceId,
                        ["mustAttack"] = a.MustAttack
                    };
                    var recipients = new JArray();
                    foreach (var dr in a.LegalDamageRecipients)
                    {
                        var drObj = new JObject { ["type"] = dr.Type.ToString() };
                        switch (dr.IdCase)
                        {
                            case DamageRecipient.IdOneofCase.PlayerSystemSeatId:
                                drObj["playerSystemSeatId"] = (int)dr.PlayerSystemSeatId;
                                break;
                            case DamageRecipient.IdOneofCase.PlaneswalkerInstanceId:
                                drObj["planeswalkerInstanceId"] = (int)dr.PlaneswalkerInstanceId;
                                break;
                            case DamageRecipient.IdOneofCase.TeamId:
                                drObj["teamId"] = (int)dr.TeamId;
                                break;
                        }
                        recipients.Add(drObj);
                    }
                    ao["legalDamageRecipients"] = recipients;
                    attackersArr.Add(ao);
                }
                resp["attackers"] = attackersArr;
                resp["can_submit"] = attackerReq.CanSubmit;
                resp["can_pass"] = false;
            }
            else if (request is CastingTimeOptionRequest castingReq)
            {
                var entries = BuildCastingTimeOptionEntries(castingReq);
                var actionsArr = new JArray();
                foreach (var entry in entries)
                {
                    actionsArr.Add(entry.Payload.DeepClone());
                }
                resp["actions"] = actionsArr;
                resp["decision_context"] = BuildCastingTimeOptionDecisionContext(entries);
            }
            else if (request is SelectTargetsRequest targetsReqForCandidates)
            {
                // Flatten TargetSelections into a structured list so the
                // Python side can see valid target instance IDs up front
                // (useful for single-candidate auto-submit and for logging
                // when a name-based lookup misses).
                var gm = GetGameManager();
                MtgGameState gsForTargets = null;
                try { gsForTargets = gm?.CurrentGameState; } catch { gsForTargets = null; }

                var selectionsArr = new JArray();
                var flatCandidates = new JArray();
                foreach (var ts in targetsReqForCandidates.TargetSelections)
                {
                    var slotTargets = new JArray();
                    foreach (var t in ts.Targets)
                    {
                        int instanceId = (int)t.TargetInstanceId;
                        uint grpId = 0;
                        if (gsForTargets != null)
                        {
                            try
                            {
                                foreach (var card in gsForTargets.Battlefield?.VisibleCards ?? System.Linq.Enumerable.Empty<MtgCardInstance>())
                                {
                                    if (card != null && card.InstanceId == instanceId)
                                    {
                                        grpId = (uint)card.GrpId;
                                        break;
                                    }
                                }
                            }
                            catch { }
                        }
                        var entry = new JObject
                        {
                            ["targetInstanceId"] = instanceId,
                            ["targetIdx"] = (int)ts.TargetIdx,
                            ["grpId"] = (int)grpId,
                        };
                        slotTargets.Add((JObject)entry.DeepClone());
                        flatCandidates.Add(entry);
                    }
                    selectionsArr.Add(new JObject
                    {
                        ["targetIdx"] = (int)ts.TargetIdx,
                        ["minTargets"] = (int)ts.MinTargets,
                        ["maxTargets"] = (int)ts.MaxTargets,
                        ["selectedTargets"] = (int)ts.SelectedTargets,
                        ["targets"] = slotTargets,
                    });
                }
                resp["target_selections"] = selectionsArr;
                resp["target_candidates"] = flatCandidates;
                resp["can_pass"] = false;
            }
            else if (request is AssignDamageRequest dmgReqExpose)
            {
                // Surface the bridge-side assigner template so Python can
                // build a structured submit_assign_damage payload by index/id
                // without re-reflecting the request shape at submit time.
                var assignersArr = new JArray();
                foreach (var assigner in dmgReqExpose.Assigners)
                {
                    var assignmentsArr = new JArray();
                    foreach (var a in assigner.Assignments)
                    {
                        assignmentsArr.Add(new JObject
                        {
                            ["instanceId"] = (int)a.InstanceId,
                            ["minDamage"] = (int)a.MinDamage,
                            ["maxDamage"] = (int)a.MaxDamage,
                            ["assignedDamage"] = (int)a.AssignedDamage,
                        });
                    }
                    assignersArr.Add(new JObject
                    {
                        ["instanceId"] = (int)assigner.InstanceId,
                        ["totalDamage"] = (int)assigner.TotalDamage,
                        ["assignments"] = assignmentsArr,
                    });
                }
                resp["assigners"] = assignersArr;
                resp["can_pass"] = false;
            }
            else if (request is SelectReplacementRequest replReqExpose)
            {
                var replacementsArr = new JArray();
                int idx = 0;
                foreach (var repl in replReqExpose.Replacements)
                {
                    replacementsArr.Add(new JObject
                    {
                        ["index"] = idx,
                        ["display"] = repl?.ToString() ?? "",
                    });
                    idx++;
                }
                resp["replacements"] = replacementsArr;
                resp["is_optional"] = replReqExpose.IsOptional;
                resp["can_pass"] = false;
            }
            else if (request is DistributionRequest distReqExpose)
            {
                var legalArr = new JArray();
                foreach (var t in distReqExpose.LegalTargetIds) legalArr.Add((int)t);
                var allArr = new JArray();
                foreach (var t in distReqExpose.TargetIds) allArr.Add((int)t);
                resp["distribution_target_ids"] = allArr;
                resp["distribution_legal_ids"] = legalArr;
                resp["distribution_min"] = (int)distReqExpose.Min;
                resp["distribution_max"] = (int)distReqExpose.Max;
                resp["distribution_min_per"] = (int)distReqExpose.MinPer;
                resp["distribution_max_per"] = (int)distReqExpose.MaxPer;
                resp["can_pass"] = false;
            }
            else if (request is SearchRequest searchReqExpose)
            {
                // Library/zone search: surface the candidate grpIds plus the
                // zones being searched. Python can pick directly from
                // search_candidates without re-resolving names.
                var optionsArr = new JArray();
                foreach (var gid in searchReqExpose.Options) optionsArr.Add((int)gid);
                var zonesArr = new JArray();
                foreach (var z in searchReqExpose.ZonesToSearch) zonesArr.Add((int)z);
                var addlZonesArr = new JArray();
                foreach (var z in searchReqExpose.AdditionalZones) addlZonesArr.Add((int)z);
                var ctxArr = new JArray();
                foreach (var c in searchReqExpose.ContextOptions) ctxArr.Add((int)c);
                resp["search_candidates"] = optionsArr;
                resp["search_zones"] = zonesArr;
                resp["search_additional_zones"] = addlZonesArr;
                resp["search_context_options"] = ctxArr;
                resp["search_is_multi_zone"] = searchReqExpose.IsMultiZoneSearch;
                resp["can_pass"] = false;
            }
            else if (request is SelectNRequest selectNReqExpose)
            {
                // Generic "choose N from a set" decision. The Ids list holds
                // the candidate instance/grp/etc. IDs (interpretation
                // governed by IdType), and MinSel/MaxSel bound the selection
                // size. ShouldCancel == true means submitting an empty
                // selection (SubmitArbitrary) is a legal "skip" path.
                var idsArr = new JArray();
                foreach (var id in selectNReqExpose.Ids) idsArr.Add((int)id);
                var zoneIdsArr = new JArray();
                foreach (var z in selectNReqExpose.ZoneIds) zoneIdsArr.Add((int)z);
                resp["select_n_ids"] = idsArr;
                resp["select_n_zone_ids"] = zoneIdsArr;
                resp["select_n_id_type"] = selectNReqExpose.IdType.ToString();
                resp["select_n_list_type"] = selectNReqExpose.ListType.ToString();
                resp["select_n_context"] = selectNReqExpose.Context.ToString();
                resp["select_n_option_context"] = selectNReqExpose.OptionContext.ToString();
                resp["select_n_min"] = selectNReqExpose.MinSel;
                resp["select_n_max"] = (int)selectNReqExpose.MaxSel;
                resp["select_n_can_cancel"] = selectNReqExpose.ShouldCancel;
                // Useful shape flags so Python doesn't have to recompute.
                resp["select_n_is_instance_id"] = selectNReqExpose.IsInstanceIdSelection;
                resp["select_n_is_zone"] = selectNReqExpose.IsZoneSelection;
                resp["select_n_is_mana_color"] = selectNReqExpose.IsManaColorSelection;
                resp["select_n_is_card_color"] = selectNReqExpose.IsCardColorSelection;
                resp["select_n_is_counter"] = selectNReqExpose.IsCounterSelection;
                resp["select_n_is_basic_land"] = selectNReqExpose.IsBasicLandSelection;
                resp["select_n_is_triggered_ability"] = selectNReqExpose.IsTriggeredAbilitySelection;
                resp["select_n_is_stacking_decision"] = selectNReqExpose.IsStackingDecision;
                resp["select_n_should_cancel"] = selectNReqExpose.ShouldCancel;
                resp["can_pass"] = false;
            }
            else if (request is GroupRequest groupReqExpose)
            {
                // Grouping / ordering decisions. InstanceIds is the pool to
                // assign; GroupSpecs describes the slot constraints.
                var instanceArr = new JArray();
                foreach (var iid in groupReqExpose.InstanceIds) instanceArr.Add((int)iid);
                // GroupSpecification's concrete fields aren't part of the
                // decompiled surface we have committed to the repo, so
                // serialize each spec via reflection over its public
                // properties — Python gets whatever Min/Max/etc. fields
                // the runtime exposes.
                var groupSpecsArr = new JArray();
                foreach (var spec in groupReqExpose.GroupSpecs)
                {
                    if (spec == null)
                    {
                        groupSpecsArr.Add(new JObject());
                        continue;
                    }
                    var specObj = new JObject();
                    foreach (var p in spec.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
                    {
                        if (!p.CanRead || p.GetIndexParameters().Length != 0) continue;
                        try
                        {
                            var token = SerializePendingRequestValue(p.GetValue(spec, null), 0);
                            if (token != null) specObj[ToCamelCase(p.Name)] = token;
                        }
                        catch { }
                    }
                    groupSpecsArr.Add(specObj);
                }
                resp["group_instance_ids"] = instanceArr;
                resp["group_specs"] = groupSpecsArr;
                resp["group_context"] = groupReqExpose.Context.ToString();
                resp["can_pass"] = false;
            }
            else if (request is NumericInputRequest numReqExpose)
            {
                // Standalone "choose a number" decisions (X-value outside
                // CastingTimeOption_NumericInputRequest).
                var disallowedArr = new JArray();
                foreach (var v in numReqExpose.DisallowedValues) disallowedArr.Add((int)v);
                var suggestedArr = new JArray();
                foreach (var v in numReqExpose.SuggestedValues) suggestedArr.Add((int)v);
                resp["numeric_min"] = (int)numReqExpose.Min;
                resp["numeric_max"] = (int)numReqExpose.Max;
                resp["numeric_input_type"] = numReqExpose.InputType.ToString();
                resp["numeric_disallowed"] = disallowedArr;
                resp["numeric_suggested"] = suggestedArr;
                resp["numeric_disallow_even"] = numReqExpose.DisallowEven;
                resp["numeric_disallow_odd"] = numReqExpose.DisallowOdd;
                resp["can_pass"] = false;
            }

            var requestPayload = BuildPendingRequestPayload(request);
            if (requestPayload.Count > 0)
            {
                resp["request_payload"] = requestPayload;
            }

            if (resp["decision_context"] == null)
            {
                var decisionContext = BuildPendingRequestDecisionContext(request, requestPayload);
                if (decisionContext != null && decisionContext.Count > 0)
                    resp["decision_context"] = decisionContext;
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

            int actionIndex = cmd.Json.Value<int>("action_index");

            if (request is ActionsAvailableRequest actionsReq)
            {
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
            else if (request is CastingTimeOptionRequest castingReq)
            {
                var entries = BuildCastingTimeOptionEntries(castingReq);
                if (actionIndex < 0 || actionIndex >= entries.Count)
                {
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Casting-time option index {actionIndex} out of range (0-{entries.Count - 1})"
                    });
                    return;
                }

                var entry = entries[actionIndex];
                string choiceKind = entry.Payload.Value<string>("choiceKind") ?? "unknown";
                int submittedGrpId = entry.Payload.Value<int?>("grpId") ?? 0;
                _log.LogInfo($"Submitting casting-time option [{actionIndex}]: {choiceKind} grpId={submittedGrpId}");

                entry.Submit();

                lock (_interactionLock)
                {
                    _lastKnownRequest = null;
                }

                var resp = new JObject
                {
                    ["ok"] = true,
                    ["submitted_type"] = "CastingTimeOption",
                    ["submitted_choice_kind"] = choiceKind,
                    ["submitted_grp_id"] = submittedGrpId
                };

                var optionIndex = entry.Payload.Value<int?>("optionIndex");
                if (optionIndex.HasValue)
                    resp["submitted_option_index"] = optionIndex.Value;

                cmd.SetResponse(resp);
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

        private void HandleSubmitBlockers(PipeCommand cmd)
        {
            // Always get fresh reference — cached _lastKnownRequest may have
            // a stale OnSubmit callback that the workflow no longer listens to.
            var request = FindPendingInteraction();
            if (request == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No pending interaction" });
                return;
            }

            if (request is DeclareBlockersRequest blockersReq)
            {
                var assignments = cmd.Json["assignments"] as JArray;
                if (assignments == null || assignments.Count == 0)
                {
                    // No blocks — submit empty (like Goldfish bot)
                    _log.LogInfo("SubmitBlockers: no blocks");
                    blockersReq.SubmitBlockers();
                }
                else
                {
                    // Match blocker instance IDs to the request's own AllBlockers
                    // objects, set SelectedAttackerInstanceIds, then submit.
                    var selectedBlockers = new List<Blocker>();
                    foreach (var a in assignments)
                    {
                        uint blockerInstanceId = (uint)a.Value<int>("blockerInstanceId");
                        var attackerIds = a["attackerInstanceIds"] as JArray;

                        // Find matching blocker from request's AllBlockers
                        Blocker matched = null;
                        foreach (var b in blockersReq.AllBlockers)
                        {
                            if (b.BlockerInstanceId == blockerInstanceId)
                            {
                                matched = b;
                                break;
                            }
                        }

                        if (matched != null)
                        {
                            // Set which attackers this blocker blocks
                            matched.SelectedAttackerInstanceIds.Clear();
                            if (attackerIds != null)
                                foreach (var aid in attackerIds)
                                    matched.SelectedAttackerInstanceIds.Add((uint)aid.Value<int>());
                            selectedBlockers.Add(matched);
                            _log.LogInfo($"UpdateBlockers: blocker={blockerInstanceId} blocking={string.Join(",", matched.SelectedAttackerInstanceIds)}");
                        }
                        else
                        {
                            _log.LogWarning($"Blocker instanceId={blockerInstanceId} not found in AllBlockers");
                        }
                    }

                    if (selectedBlockers.Count > 0)
                    {
                        // BaseUserRequest._outboundMessage is shared mutable
                        // state — calling UpdateBlockers() then SubmitBlockers()
                        // back-to-back lets the second message overwrite the
                        // first before MTGA serializes it, so the pairings
                        // never reach the server. Build two fresh
                        // ClientToGREMessage instances and invoke OnSubmit
                        // directly (same fix as SelectTargets in v2.2.2).
                        var declMsg = new ClientToGREMessage
                        {
                            Type = ClientMessageType.DeclareBlockersResp,
                            GameStateId = blockersReq.OriginalMessage.GameStateId,
                            RespId = blockersReq.OriginalMessage.MsgId,
                            DeclareBlockersResp = new DeclareBlockersResp(),
                        };
                        foreach (var b in selectedBlockers)
                            declMsg.DeclareBlockersResp.SelectedBlockers.Add(b);
                        _log.LogInfo($"UpdateBlockers (direct): {selectedBlockers.Count} pairings");
                        blockersReq.OnSubmit?.Invoke(declMsg);

                        var commitMsg = new ClientToGREMessage
                        {
                            Type = ClientMessageType.SubmitBlockersReq,
                            GameStateId = blockersReq.OriginalMessage.GameStateId,
                            RespId = blockersReq.OriginalMessage.MsgId,
                        };
                        _log.LogInfo("SubmitBlockers (direct): finalizing");
                        blockersReq.OnSubmit?.Invoke(commitMsg);
                    }
                    else
                    {
                        var commitMsg = new ClientToGREMessage
                        {
                            Type = ClientMessageType.SubmitBlockersReq,
                            GameStateId = blockersReq.OriginalMessage.GameStateId,
                            RespId = blockersReq.OriginalMessage.MsgId,
                        };
                        _log.LogInfo("SubmitBlockers (direct): no pairings, finalizing");
                        blockersReq.OnSubmit?.Invoke(commitMsg);
                    }
                }

                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "DeclareBlockers" });
            }
            else
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = $"Pending request is {request.GetType().Name}, not DeclareBlockersRequest"
                });
            }
        }

        private void HandleSubmitAttackers(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No pending interaction" });
                return;
            }

            if (request is DeclareAttackerRequest attackerReq)
            {
                var attackerList = cmd.Json["attackers"] as JArray;
                if (attackerList == null || attackerList.Count == 0)
                {
                    // No attackers specified — finalize (submit current declarations).
                    // This is the second step of the two-step flow, or "attack with nobody".
                    _log.LogInfo("SubmitAttackers: finalizing (no new attackers)");
                    attackerReq.SubmitAttackers();
                    lock (_interactionLock) { _lastKnownRequest = null; }
                    cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "DeclareAttackersSubmit" });
                }
                else
                {
                    // Two-step attacker declaration (matches DeclareAttackerRequestNPEHandler):
                    // Step 1: Find attackers in request.Attackers (NOT QualifiedAttackers),
                    //         set SelectedDamageRecipient, call UpdateAttacker.
                    // Step 2: Python calls again with empty list → SubmitAttackers() to finalize.
                    var requestedIds = new HashSet<uint>();
                    foreach (var a in attackerList)
                        requestedIds.Add((uint)a.Value<int>("attackerInstanceId"));

                    var matched = new System.Collections.Generic.List<Attacker>();
                    foreach (var a in attackerReq.Attackers)
                    {
                        if (requestedIds.Contains(a.AttackerInstanceId)
                            && a.SelectedDamageRecipient == null
                            && a.LegalDamageRecipients.Count > 0)
                        {
                            a.SelectedDamageRecipient = a.LegalDamageRecipients[0];
                            matched.Add(a);
                        }
                    }

                    if (matched.Count > 0)
                    {
                        _log.LogInfo($"UpdateAttacker: declaring {matched.Count} attackers (needs finalize)");
                        attackerReq.UpdateAttacker(matched.ToArray());
                        lock (_interactionLock) { _lastKnownRequest = null; }
                        cmd.SetResponse(new JObject
                        {
                            ["ok"] = true,
                            ["submitted_type"] = "DeclareAttackersUpdate",
                            ["needs_finalize"] = true,
                            ["declared_count"] = matched.Count
                        });
                    }
                    else
                    {
                        // Requested IDs not found or already declared — finalize
                        _log.LogInfo($"SubmitAttackers: requested IDs already declared or not found, finalizing");
                        attackerReq.SubmitAttackers();
                        lock (_interactionLock) { _lastKnownRequest = null; }
                        cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "DeclareAttackersSubmit" });
                    }
                }
            }
            else
            {
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = $"Pending request is {request.GetType().Name}, not DeclareAttackerRequest"
                });
            }
        }

        // -------------------------------------------------------------------
        // Generic decision handlers (mulligan, starting player, selection, etc.)
        // -------------------------------------------------------------------

        private void HandleSubmitMulligan(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is MulliganRequest mulliganReq)
            {
                bool keep = cmd.Json.Value<bool>("keep");
                if (keep)
                {
                    _log.LogInfo("Submitting mulligan: KEEP");
                    mulliganReq.KeepHand();
                }
                else
                {
                    _log.LogInfo("Submitting mulligan: MULLIGAN");
                    mulliganReq.MulliganHand();
                }
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = keep ? "Keep" : "Mulligan" });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not MulliganRequest" });
            }
        }

        private void HandleSubmitChooseStartingPlayer(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is ChooseStartingPlayerRequest chooseReq)
            {
                uint seatId = (uint)cmd.Json.Value<int>("seat_id");
                _log.LogInfo($"Submitting choose starting player: seat {seatId}");
                chooseReq.ChooseStartingPlayer(seatId);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "ChooseStartingPlayer", ["seat_id"] = (int)seatId });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not ChooseStartingPlayerRequest" });
            }
        }

        private void HandleSubmitSelection(PipeCommand cmd)
        {
            var request = FindPendingInteraction();

            if (request is SelectNRequest selectNReq)
            {
                var idsArr = cmd.Json["ids"] as JArray;
                if (idsArr == null || idsArr.Count == 0)
                {
                    _log.LogInfo("Submitting SelectN: arbitrary/empty");
                    selectNReq.SubmitArbitrary();
                }
                else
                {
                    var ids = new List<uint>();
                    foreach (var id in idsArr) ids.Add((uint)id.Value<int>());
                    _log.LogInfo($"Submitting SelectN: {ids.Count} selections");
                    selectNReq.SubmitSelection(ids);
                }
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectN" });
            }
            else if (request is SearchRequest searchReq)
            {
                var idsArr = cmd.Json["ids"] as JArray;
                var ids = new List<uint>();
                if (idsArr != null)
                    foreach (var id in idsArr) ids.Add((uint)id.Value<int>());
                _log.LogInfo($"Submitting Search: {ids.Count} selections");
                searchReq.SubmitSelection(ids);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Search" });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectN/Search" });
            }
        }

        private void HandleSubmitGroup(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is GroupRequest groupReq)
            {
                var groupsArr = cmd.Json["groups"] as JArray;
                var groups = new List<Group>();
                if (groupsArr != null)
                {
                    foreach (var g in groupsArr)
                    {
                        var group = new Group();
                        var idsArr = g["ids"] as JArray;
                        if (idsArr != null)
                            foreach (var id in idsArr) group.Ids.Add((uint)id.Value<int>());

                        // Optional zone/sub_zone fields so the caller can
                        // express scry/surveil-style top/bottom ordering.
                        // Accept either the short enum name ("Library",
                        // "Top") or the protobuf OriginalName form
                        // ("ZoneType_Library", "SubZoneType_Top").
                        var zoneTok = g["zone"];
                        if (zoneTok != null && zoneTok.Type != JTokenType.Null)
                        {
                            var raw = zoneTok.Value<string>() ?? string.Empty;
                            var stripped = raw.StartsWith("ZoneType_") ? raw.Substring("ZoneType_".Length) : raw;
                            ZoneType zt;
                            if (Enum.TryParse<ZoneType>(stripped, true, out zt))
                                group.ZoneType = zt;
                        }
                        var subZoneTok = g["sub_zone"];
                        if (subZoneTok != null && subZoneTok.Type != JTokenType.Null)
                        {
                            var raw = subZoneTok.Value<string>() ?? string.Empty;
                            var stripped = raw.StartsWith("SubZoneType_") ? raw.Substring("SubZoneType_".Length) : raw;
                            SubZoneType szt;
                            if (Enum.TryParse<SubZoneType>(stripped, true, out szt))
                                group.SubZoneType = szt;
                        }

                        groups.Add(group);
                    }
                }
                _log.LogInfo($"Submitting Group: {groups.Count} groups");
                groupReq.SubmitGroups(groups);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Group" });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not GroupRequest" });
            }
        }

        private void HandleSubmitOptional(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is OptionalActionMessageRequest optionalReq)
            {
                bool accept = cmd.Json.Value<bool>("accept");
                var response = accept ? OptionResponse.AllowYes : OptionResponse.CancelNo;
                _log.LogInfo($"Submitting Optional: {response}");
                optionalReq.SubmitResponse(response);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Optional", ["response"] = response.ToString() });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not OptionalActionMessageRequest" });
            }
        }

        private void HandleSubmitNumeric(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is NumericInputRequest numericReq)
            {
                uint value = (uint)cmd.Json.Value<int>("value");
                _log.LogInfo($"Submitting NumericInput: {value}");
                numericReq.SubmitValue(value);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "NumericInput", ["value"] = (int)value });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not NumericInputRequest" });
            }
        }

        private void HandleSubmitTargets(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request is SelectTargetsRequest targetsReq)
            {
                // Python may send a single id (target_instance_id, legacy) or a
                // full per-slot list (target_instance_ids) for multi-target
                // requests — e.g. an Aura that enchants your creature AND exiles
                // an opponent's permanent. Each slot gets at most one of these
                // ids; if none is legal for a slot we fall back to that slot's
                // first legal target. Submitting one id for a 2-slot request
                // left the other slot empty → MTGA rejected → the request
                // re-presented and the autopilot wedged (Sheltered by Ghosts /
                // Ethereal Armor). Cover every slot.
                var preferredIds = new List<uint>();
                var idsTok = cmd.Json["target_instance_ids"];
                if (idsTok is JArray idsArr)
                {
                    foreach (var v in idsArr)
                    {
                        var pid = (uint)(int)v;
                        if (pid != 0 && !preferredIds.Contains(pid)) preferredIds.Add(pid);
                    }
                }
                var singleTok = cmd.Json["target_instance_id"];
                if (singleTok != null && singleTok.Type != JTokenType.Null)
                {
                    var pid = (uint)singleTok.Value<int>();
                    if (pid != 0 && !preferredIds.Contains(pid)) preferredIds.Add(pid);
                }
                uint targetInstanceId = preferredIds.Count > 0 ? preferredIds[0] : 0;
                // Default to finalizing (UpdateTarget + SubmitTargets) so the
                // "Take Action" / confirm button is pressed automatically.
                // Callers that need to pair multiple targets before committing
                // can pass "finalize": false on intermediate calls.
                bool finalize = true;
                var finalizeTok = cmd.Json["finalize"];
                if (finalizeTok != null && finalizeTok.Type != JTokenType.Null)
                    finalize = finalizeTok.Value<bool>();

                // MTGA's SelectTargetsRequest can carry MULTIPLE TargetSelections
                // (e.g. Ethereal Armor surfaces a metadata slot at idx 0 plus the
                // enchant slot at idx 1). UpdateTarget builds a SelectTargetsResp
                // for ONE slot at a time and calls Submit() per slot, then
                // SubmitTargets() commits. If we fill only one slot when multiple
                // are required, MTGA silently rejects and re-presents the same
                // request — autopilot then loops forever (issue observed live
                // 2026-04-30 with Ethereal Armor and room benefit triggers).
                // Strategy: try caller's instance_id first per slot; if it
                // doesn't appear in a slot's legal Targets, fall back to the
                // slot's first legal target. Fill every TargetSelection that
                // has at least one legal target before finalizing.
                // Diagnostic: dump full TargetSelections shape so we can see
                // exactly what MTGA is asking for (idx / min / max / selected /
                // legal-targets) when submissions don't advance the request.
                var diagSlots = new List<string>();
                foreach (var ts in targetsReq.TargetSelections)
                {
                    var ids = new List<string>();
                    foreach (var t in ts.Targets) ids.Add($"{t.TargetInstanceId}");
                    diagSlots.Add($"idx={ts.TargetIdx} min={ts.MinTargets} max={ts.MaxTargets} sel={ts.SelectedTargets} legal=[{string.Join(",", ids)}]");
                }
                _log.LogInfo($"SelectTargets shape (callers=[{string.Join(",", preferredIds)}]): {diagSlots.Count} slot(s) | {string.Join(" | ", diagSlots)}");

                var filledSlots = new List<uint>();
                var usedIds = new HashSet<uint>();
                bool callerMatched = false;
                int requiredSlots = 0;
                int requiredFilled = 0;
                foreach (var ts in targetsReq.TargetSelections)
                {
                    bool slotRequired = ts.MinTargets > 0 && ts.SelectedTargets < ts.MinTargets;
                    if (slotRequired) requiredSlots++;

                    Target chosen = null;
                    // Prefer one of Python's ids that is legal in THIS slot and
                    // not already consumed by another slot.
                    foreach (var pid in preferredIds)
                    {
                        if (usedIds.Contains(pid)) continue;
                        foreach (var t in ts.Targets)
                        {
                            if (t.TargetInstanceId == pid)
                            {
                                chosen = t;
                                callerMatched = true;
                                break;
                            }
                        }
                        if (chosen != null) break;
                    }
                    if (chosen == null)
                    {
                        // Fall back to this slot's first legal, unused target.
                        foreach (var t in ts.Targets)
                        {
                            if (!usedIds.Contains(t.TargetInstanceId))
                            {
                                chosen = t;
                                break;
                            }
                        }
                    }
                    if (chosen == null)
                    {
                        // Slot has zero free legal targets — skip; SubmitTargets
                        // will accept (if MinTargets=0) or reject, and we report
                        // the partial fill honestly below.
                        continue;
                    }
                    usedIds.Add(chosen.TargetInstanceId);
                    if (slotRequired) requiredFilled++;
                    // Build a FRESH SelectTargetsResp message and invoke OnSubmit
                    // directly. SelectTargetsRequest.UpdateTarget mutates the
                    // request's shared _outboundMessage and calls Submit() —
                    // which passes the SAME REFERENCE to MTGA's GRE pipeline.
                    // When we then call SubmitTargets, it mutates that shared
                    // buffer AGAIN before MTGA finishes processing the first
                    // message, corrupting the SelectTargetsResp into a
                    // SubmitTargetsReq. Result: MTGA sees "Submit with 0
                    // targets" and rejects, the request re-presents, and
                    // autopilot loops forever (Ethereal Armor / Feather of
                    // Flight observed live 2026-04-30 with v2.2.1 plugin).
                    //
                    // Cancel/Undo/Concede in BaseUserRequest already build
                    // fresh ClientToGREMessage instances and invoke OnSubmit
                    // directly — we mirror that pattern here. LegalAction =
                    // Select marks the target as actually selected (request
                    // copies of legal targets default to None).
                    var sentTarget = new Target
                    {
                        TargetInstanceId = chosen.TargetInstanceId,
                        LegalAction = SelectAction.Select,
                        Highlight = chosen.Highlight,
                    };
                    var respMsg = new ClientToGREMessage
                    {
                        Type = ClientMessageType.SelectTargetsResp,
                        GameStateId = targetsReq.OriginalMessage.GameStateId,
                        RespId = targetsReq.OriginalMessage.MsgId,
                        SelectTargetsResp = new SelectTargetsResp
                        {
                            Target = new TargetSelection
                            {
                                TargetIdx = ts.TargetIdx,
                                Targets = { sentTarget },
                            },
                        },
                    };
                    _log.LogInfo($"UpdateTarget (direct): instanceId={sentTarget.TargetInstanceId}, targetIdx={ts.TargetIdx}, action=Select, finalize={finalize}");
                    targetsReq.OnSubmit?.Invoke(respMsg);
                    filledSlots.Add(ts.TargetIdx);
                }

                // If all slots are already satisfied (selected >= min), skip
                // SelectTargetsResp and just finalize. Avoids a race where
                // the game state ID changes between the two OnSubmit calls,
                // making SubmitTargetsReq's GameStateId stale — observed live
                // 2026-06-18: Scales of Shale targeting loop.
                bool allAlreadySatisfied = true;
                foreach (var ts in targetsReq.TargetSelections)
                {
                    if (ts.MinTargets > 0 && ts.SelectedTargets < ts.MinTargets)
                    {
                        allAlreadySatisfied = false;
                        break;
                    }
                }

                if (allAlreadySatisfied && finalize && filledSlots.Count == 0)
                {
                    _log.LogInfo("All targets already selected — sending SubmitTargetsReq only");
                    var commitMsg = new ClientToGREMessage
                    {
                        Type = ClientMessageType.SubmitTargetsReq,
                        GameStateId = targetsReq.OriginalMessage.GameStateId,
                        RespId = targetsReq.OriginalMessage.MsgId,
                    };
                    targetsReq.OnSubmit?.Invoke(commitMsg);
                    lock (_interactionLock) { _lastKnownRequest = null; }
                    cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectTargets", ["target_instance_id"] = (int)targetInstanceId, ["already_selected"] = true, ["finalized"] = true });
                    return;
                }

                if (filledSlots.Count == 0)
                {
                    var legalIds = new List<string>();
                    foreach (var ts in targetsReq.TargetSelections)
                        foreach (var t in ts.Targets)
                            legalIds.Add($"{t.TargetInstanceId}");
                    _log.LogWarning($"No slots filled (caller={targetInstanceId}, legal=[{string.Join(",", legalIds)}])");
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"No legal targets to fill" });
                    return;
                }

                if (!callerMatched)
                {
                    _log.LogWarning($"Caller ids [{string.Join(",", preferredIds)}] matched no slot; used per-slot defaults for {filledSlots.Count} slot(s)");
                }

                // Honest commit: only finalize and report success when every
                // REQUIRED slot got a target. Committing a partial selection
                // (one slot of a two-target Aura) makes MTGA reject and
                // re-present the request; reporting ok=true + nulling our cached
                // request told Python it was consumed when the spell was still
                // wedged on the stack, so the autopilot abandoned it.
                bool allRequiredFilled = requiredFilled >= requiredSlots;

                if (finalize && allRequiredFilled)
                {
                    // Two-phase commit. The GRE round-trips an UPDATED
                    // SelectTargetsReq (new GameStateId + MsgId, SelectedTargets
                    // bumped) after it processes our SelectTargetsResp; the real
                    // client only sends SubmitTargetsReq from that updated
                    // request (SelectTargetsWorkflow.ApplyInteractionInternal →
                    // CanAutoSubmitTargets → SubmitTargets). Committing here
                    // immediately with the ORIGINAL ids raced that round-trip:
                    // the stale SubmitTargetsReq was dropped server-side, the
                    // client workflow dismissed (has_pending went false), the
                    // server kept waiting, and the cast rolled back on the
                    // timer. Observed 2026-07-01 with Depower / Patriar's
                    // Humiliation / Swords to Plowshares — always with 2+
                    // legal candidates, because a sole candidate is
                    // auto-targeted client-side and never reaches this path.
                    var host = MtgaCoachHost.Instance;
                    if (host != null)
                    {
                        host.StartCoroutine(DeferredSubmitTargets(
                            cmd, targetsReq, (int)targetInstanceId,
                            filledSlots.Count, requiredSlots, requiredFilled));
                        return;
                    }
                    // No host (shouldn't happen): legacy immediate commit is
                    // still better than dropping the interaction.
                    var commitMsg = new ClientToGREMessage
                    {
                        Type = ClientMessageType.SubmitTargetsReq,
                        GameStateId = targetsReq.OriginalMessage.GameStateId,
                        RespId = targetsReq.OriginalMessage.MsgId,
                    };
                    _log.LogInfo($"SubmitTargets (direct): finalizing ({filledSlots.Count} slot(s)) [no-host fallback]");
                    targetsReq.OnSubmit?.Invoke(commitMsg);
                }
                else if (finalize)
                {
                    _log.LogWarning($"SubmitTargets: NOT finalizing — only {requiredFilled}/{requiredSlots} required slots filled; leaving request open for retry");
                }

                if (allRequiredFilled)
                {
                    lock (_interactionLock) { _lastKnownRequest = null; }
                }
                cmd.SetResponse(new JObject
                {
                    ["ok"] = allRequiredFilled,
                    ["submitted_type"] = "SelectTargets",
                    ["target_instance_id"] = (int)targetInstanceId,
                    ["slots_required"] = requiredSlots,
                    ["slots_filled"] = filledSlots.Count,
                    ["required_filled"] = requiredFilled,
                    ["finalized"] = finalize && allRequiredFilled,
                });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectTargetsRequest" });
            }
        }

        /// <summary>
        /// Phase 2 of target submission: wait (frame-polled, main thread) for
        /// the GRE's updated SelectTargetsReq that acknowledges our
        /// SelectTargetsResp, then send SubmitTargetsReq stamped with THAT
        /// request's GameStateId/MsgId. Mirrors the real client's
        /// click → server round-trip → auto-submit sequence.
        /// </summary>
        private IEnumerator DeferredSubmitTargets(
            PipeCommand cmd,
            SelectTargetsRequest originalReq,
            int callerTargetId,
            int slotsFilled,
            int requiredSlots,
            int requiredFilled)
        {
            // Budget must fit inside Python's 8s submit_targets pipe timeout.
            // 1.2s (was 3.0): when the GRE does round-trip an updated request
            // it lands within a few frames; multi-slot fills we authored
            // ourselves often get no round-trip at all and were eating the
            // full window before the (correct) legacy commit fired — observed
            // live 2026-07-02 as back-to-back 3s stalls on Nesting Grounds.
            const float timeoutS = 1.2f;
            const float goneGraceS = 0.6f;
            float start = Time.unscaledTime;
            float goneSince = -1f;
            uint sourceId = originalReq.SourceId;

            JObject BuildResponse(bool ok, bool finalized, bool advanced)
            {
                return new JObject
                {
                    ["ok"] = ok,
                    ["submitted_type"] = "SelectTargets",
                    ["target_instance_id"] = callerTargetId,
                    ["slots_required"] = requiredSlots,
                    ["slots_filled"] = slotsFilled,
                    ["required_filled"] = requiredFilled,
                    ["finalized"] = finalized,
                    ["advanced_without_commit"] = advanced,
                };
            }

            while (Time.unscaledTime - start < timeoutS)
            {
                yield return null;

                SelectTargetsRequest current = null;
                try
                {
                    current = FindPendingInteraction() as SelectTargetsRequest;
                }
                catch (Exception ex)
                {
                    _log.LogWarning($"DeferredSubmitTargets: poll failed: {ex.Message}");
                }

                if (current == null || current.SourceId != sourceId)
                {
                    // Request no longer presented — either the client already
                    // auto-submitted once the selection landed, or the workflow
                    // is mid-update. Give it a grace window before declaring
                    // the interaction consumed.
                    if (goneSince < 0f) goneSince = Time.unscaledTime;
                    if (Time.unscaledTime - goneSince >= goneGraceS)
                    {
                        _log.LogInfo("DeferredSubmitTargets: request no longer pending — selection consumed");
                        lock (_interactionLock) { _lastKnownRequest = null; }
                        cmd.SetResponse(BuildResponse(ok: true, finalized: true, advanced: true));
                        yield break;
                    }
                    continue;
                }
                goneSince = -1f;

                if (ReferenceEquals(current, originalReq))
                    continue; // GRE hasn't round-tripped the selection yet

                bool satisfied = true;
                foreach (var ts in current.TargetSelections)
                {
                    if (ts.MinTargets > 0 && ts.SelectedTargets < ts.MinTargets)
                    {
                        satisfied = false;
                        break;
                    }
                }
                if (!satisfied)
                    continue;

                var commitMsg = new ClientToGREMessage
                {
                    Type = ClientMessageType.SubmitTargetsReq,
                    GameStateId = current.OriginalMessage.GameStateId,
                    RespId = current.OriginalMessage.MsgId,
                };
                _log.LogInfo(
                    "DeferredSubmitTargets: committing via updated request " +
                    $"(gameStateId={current.OriginalMessage.GameStateId}, msgId={current.OriginalMessage.MsgId})");
                current.OnSubmit?.Invoke(commitMsg);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(BuildResponse(ok: true, finalized: true, advanced: false));
                yield break;
            }

            // Timeout: no updated request appeared. If the ORIGINAL request is
            // still the pending one, its GameStateId is still current (the GRE
            // never round-tripped), so the legacy immediate commit is correct
            // here — this is the single-phase shape that historically worked.
            SelectTargetsRequest lastSeen = null;
            try { lastSeen = FindPendingInteraction() as SelectTargetsRequest; }
            catch (Exception) { }
            var commitTarget = (lastSeen != null && lastSeen.SourceId == sourceId) ? lastSeen : originalReq;
            var lateCommit = new ClientToGREMessage
            {
                Type = ClientMessageType.SubmitTargetsReq,
                GameStateId = commitTarget.OriginalMessage.GameStateId,
                RespId = commitTarget.OriginalMessage.MsgId,
            };
            _log.LogWarning(
                "DeferredSubmitTargets: no updated request within " +
                $"{timeoutS:0.0}s — committing with last-seen ids " +
                $"(gameStateId={commitTarget.OriginalMessage.GameStateId})");
            commitTarget.OnSubmit?.Invoke(lateCommit);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(BuildResponse(ok: true, finalized: true, advanced: false));
        }

        // -------------------------------------------------------------------
        // Phase 2/3/4 handlers: full BaseUserRequest coverage so autopilot
        // never has to fall back to vision/manual for a request type the
        // bridge can authoritatively submit.
        // -------------------------------------------------------------------

        private void HandleSubmitAssignDamage(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is AssignDamageRequest dmgReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not AssignDamageRequest" });
                return;
            }

            // Expected payload: {"assigners": [{"instanceId": <attacker>, "assignments": [{"instanceId": <receiver>, "damage": <int>}, ...]}, ...]}
            var assignersJson = cmd.Json["assigners"] as JArray;
            if (assignersJson == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "Missing 'assigners' array" });
                return;
            }

            var built = new List<DamageAssigner>();
            foreach (var aJson in assignersJson)
            {
                uint attackerId = (uint)(int)aJson["instanceId"];
                var existing = dmgReq.Assigners.Find(x => x.InstanceId == attackerId);
                if (existing == null)
                {
                    _log.LogWarning($"submit_assign_damage: attacker {attackerId} not in request");
                    continue;
                }
                // Clone existing assigner and overwrite assignments with caller-supplied damage.
                var newAssigner = new DamageAssigner
                {
                    InstanceId = existing.InstanceId,
                    TotalDamage = existing.TotalDamage,
                };
                var assignmentsArr = aJson["assignments"] as JArray;
                if (assignmentsArr != null)
                {
                    foreach (var asJson in assignmentsArr)
                    {
                        uint receiverId = (uint)(int)asJson["instanceId"];
                        uint damage = (uint)(int)asJson["damage"];
                        DamageAssignment template = null;
                        foreach (var a in existing.Assignments)
                        {
                            if (a.InstanceId == receiverId) { template = a; break; }
                        }
                        var newAssignment = new DamageAssignment
                        {
                            InstanceId = receiverId,
                            MinDamage = template?.MinDamage ?? 0,
                            MaxDamage = template?.MaxDamage ?? damage,
                            AssignedDamage = damage,
                        };
                        newAssigner.Assignments.Add(newAssignment);
                    }
                }
                built.Add(newAssigner);
            }

            _log.LogInfo($"Submitting AssignDamage: {built.Count} assigners");
            dmgReq.SubmitAssignment(built);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "AssignDamage", ["assigner_count"] = built.Count });
        }

        private void HandleSubmitDistribution(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is DistributionRequest distReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not DistributionRequest" });
                return;
            }

            // Payload: {"distributions": {"<targetInstanceId>": <amount>, ...}}
            var distJson = cmd.Json["distributions"] as JObject;
            if (distJson == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "Missing 'distributions' object" });
                return;
            }

            var dict = new Dictionary<uint, uint>();
            foreach (var prop in distJson.Properties())
            {
                if (uint.TryParse(prop.Name, out uint targetId))
                {
                    dict[targetId] = (uint)(int)prop.Value;
                }
            }

            _log.LogInfo($"Submitting Distribution: {string.Join(",", dict)}");
            distReq.SubmitDistribution(dict);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Distribution", ["target_count"] = dict.Count });
        }

        private void HandleSubmitOrder(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is OrderRequest orderReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not OrderRequest" });
                return;
            }
            var idsJson = cmd.Json["ids"] as JArray;
            var ordered = new List<uint>();
            if (idsJson != null)
            {
                foreach (var idTok in idsJson) ordered.Add((uint)(int)idTok);
            }
            else
            {
                // Default: submit current Ids order as-is
                ordered.AddRange(orderReq.Ids);
            }
            _log.LogInfo($"Submitting Order: [{string.Join(",", ordered)}]");
            orderReq.SubmitOrder(ordered);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Order", ["count"] = ordered.Count });
        }

        private void HandleSubmitSelectReplacement(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is SelectReplacementRequest replReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectReplacementRequest" });
                return;
            }
            // Payload: {"index": <int>}  — index into Replacements list.
            // Optional: {"decline": true} when the request is optional.
            bool decline = cmd.Json.Value<bool?>("decline") ?? false;
            if (decline)
            {
                if (replReq.IsOptional)
                {
                    _log.LogInfo("SelectReplacement: declining (optional)");
                    replReq.Decline();
                    lock (_interactionLock) { _lastKnownRequest = null; }
                    cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectReplacement", ["declined"] = true });
                    return;
                }
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "SelectReplacementRequest is not optional" });
                return;
            }
            int idx = cmd.Json.Value<int?>("index") ?? 0;
            if (idx < 0 || idx >= replReq.Replacements.Count)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Index {idx} out of range (0-{replReq.Replacements.Count - 1})" });
                return;
            }
            _log.LogInfo($"Submitting SelectReplacement: index {idx}");
            replReq.SubmitReplacement(replReq.Replacements[idx]);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectReplacement", ["index"] = idx });
        }

        private void HandleSubmitSelectCounters(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is SelectCountersRequest countersReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectCountersRequest" });
                return;
            }
            // Payload: {"pairs": [{"counterType": "<CounterType name>", "amount": <int>}, ...]}
            var pairsJson = cmd.Json["pairs"] as JArray;
            var pairs = new List<CounterPair>();
            if (pairsJson != null)
            {
                foreach (var p in pairsJson)
                {
                    var pair = new CounterPair();
                    var ctName = p.Value<string>("counterType");
                    int amount = p.Value<int?>("amount") ?? p.Value<int?>("count") ?? 0;
                    int instanceId = p.Value<int?>("instanceId") ?? 0;
                    if (!string.IsNullOrEmpty(ctName) && System.Enum.TryParse<CounterType>(ctName, out var ct))
                    {
                        pair.CounterType = ct;
                    }
                    pair.Count = (uint)amount;
                    if (instanceId != 0) pair.InstanceId = (uint)instanceId;
                    pairs.Add(pair);
                }
            }
            _log.LogInfo($"Submitting SelectCounters: {pairs.Count} pairs");
            countersReq.SubmitCountersResponse(pairs);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectCounters", ["pair_count"] = pairs.Count });
        }

        private void HandleSubmitStringInput(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is StringInputRequest strReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not StringInputRequest" });
                return;
            }
            string value = cmd.Json.Value<string>("value") ?? "";
            _log.LogInfo($"Submitting StringInput: '{value}'");
            strReq.SubmitValue(value);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "StringInput", ["value"] = value });
        }

        private void HandleSubmitIntermission(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is IntermissionRequest intReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not IntermissionRequest" });
                return;
            }
            // Payload: {"option": "<ClientMessageType name>"} e.g. "ConcedeReq", "NextGameReq"
            string optionName = cmd.Json.Value<string>("option") ?? "";
            if (!System.Enum.TryParse<ClientMessageType>(optionName, out var option))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Unknown ClientMessageType '{optionName}'" });
                return;
            }
            _log.LogInfo($"Submitting Intermission: {option}");
            intReq.SubmitOption(option);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Intermission", ["option"] = optionName });
        }

        private void HandleSubmitGather(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is GatherRequest gatherReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not GatherRequest" });
                return;
            }
            // Payload: {"gatherings": [{"instanceId": <int>, "amount": <int>}, ...]}
            // Each Gathering submission is a (target instance, amount) pair —
            // typically used for distributing counters/effects.
            var gatheringsJson = cmd.Json["gatherings"] as JArray;
            var gatherings = new List<Gathering>();
            if (gatheringsJson != null)
            {
                foreach (var g in gatheringsJson)
                {
                    int instanceId = g.Value<int?>("instanceId") ?? 0;
                    int amount = g.Value<int?>("amount") ?? 0;
                    var gathering = new Gathering
                    {
                        InstanceId = (uint)instanceId,
                        Amount = (uint)amount,
                    };
                    gatherings.Add(gathering);
                }
            }
            _log.LogInfo($"Submitting Gather: {gatherings.Count} gatherings");
            gatherReq.SubmitGathering(gatherings);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "Gather", ["count"] = gatherings.Count });
        }

        private void HandleSubmitAutoTap(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            // PayCostsRequest is a parent wrapper that holds an AutoTapActions
            // child request when MTGA has a pre-computed Auto Pay solution.
            // Submitting that child's solution is exactly what the in-game
            // "Auto Pay" button does — auto_respond on the parent might
            // Decline (cancel) for optional pays, which is the wrong direction.
            AutoTapActionsRequest autoTapReq = request as AutoTapActionsRequest;
            if (autoTapReq == null && request is PayCostsRequest payReq)
            {
                autoTapReq = payReq.AutoTapActions;
                if (autoTapReq == null)
                {
                    foreach (var child in payReq.ChildRequests)
                    {
                        if (child is AutoTapActionsRequest a) { autoTapReq = a; break; }
                    }
                }
            }
            if (autoTapReq == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, no AutoTapActionsRequest available" });
                return;
            }
            // Payload: optional {"solution_index": <int>} to pick from candidates;
            // default: submit the first available solution.
            int solutionIndex = cmd.Json.Value<int?>("solution_index") ?? 0;
            // AutoTapActionsRequest.Solutions is a readonly FIELD (not property).
            // Direct access works; earlier reflection via GetProperty returned
            // null and made every PayCosts auto-pay fail with "No AutoTap solution".
            if (autoTapReq.Solutions == null || autoTapReq.Solutions.Count == 0)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "AutoTap Solutions list is empty" });
                return;
            }
            if (solutionIndex < 0 || solutionIndex >= autoTapReq.Solutions.Count)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"AutoTap solution index {solutionIndex} out of range (0-{autoTapReq.Solutions.Count - 1})" });
                return;
            }
            var chosen = autoTapReq.Solutions[solutionIndex];
            _log.LogInfo($"Submitting AutoTap solution {solutionIndex} (of {autoTapReq.Solutions.Count})");
            autoTapReq.SubmitSolution(chosen);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "AutoTap", ["solution_index"] = solutionIndex });
        }

        private void HandleSubmitSelectFromGroups(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is SelectFromGroupsRequest sfgReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectFromGroupsRequest" });
                return;
            }
            // Payload: {"groups": [{"ids": [<int>...], "groupId": <int?>}, ...]}
            var groupsJson = cmd.Json["groups"] as JArray;
            var groups = new List<Group>();
            if (groupsJson != null)
            {
                foreach (var g in groupsJson)
                {
                    var grp = new Group();
                    var idsArr = g["ids"] as JArray;
                    if (idsArr != null)
                    {
                        foreach (var idTok in idsArr) grp.Ids.Add((uint)(int)idTok);
                    }
                    int groupId = g.Value<int?>("groupId") ?? 0;
                    if (groupId > 0) grp.GroupId = groupId;
                    groups.Add(grp);
                }
            }
            _log.LogInfo($"Submitting SelectFromGroups: {groups.Count} groups");
            sfgReq.Submit(groups);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectFromGroups", ["group_count"] = groups.Count });
        }

        private void HandleSubmitSelectNGroup(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is SelectNGroupRequest sngReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SelectNGroupRequest" });
                return;
            }
            // Payload: {"ids": [<int>, ...]} or {"id": <int>}
            var idsJson = cmd.Json["ids"] as JArray;
            if (idsJson != null)
            {
                var ids = new List<uint>();
                foreach (var idTok in idsJson) ids.Add((uint)(int)idTok);
                _log.LogInfo($"Submitting SelectNGroup: ids={string.Join(",", ids)}");
                sngReq.SubmitGroupSelection(ids);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectNGroup", ["count"] = ids.Count });
                return;
            }
            int singleId = cmd.Json.Value<int?>("id") ?? 0;
            _log.LogInfo($"Submitting SelectNGroup: id={singleId}");
            sngReq.SubmitGroupSelection((uint)singleId);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SelectNGroup", ["id"] = singleId });
        }

        private void HandleSubmitSearchFromGroups(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (!(request is SearchFromGroupsRequest sfgReq))
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, not SearchFromGroupsRequest" });
                return;
            }
            // Payload: either {"zone": <int>} to pick a search zone,
            // or {"groups": [...]} to submit a selection (same shape as SelectFromGroups).
            var zoneTok = cmd.Json["zone"];
            if (zoneTok != null && zoneTok.Type != JTokenType.Null)
            {
                uint zone = (uint)(int)zoneTok;
                _log.LogInfo($"SubmitSearchFromGroups: zone {zone}");
                sfgReq.SubmitZone(zone);
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SearchFromGroups", ["zone"] = (int)zone });
                return;
            }
            var groupsJson = cmd.Json["groups"] as JArray;
            var groups = new List<Group>();
            if (groupsJson != null)
            {
                foreach (var g in groupsJson)
                {
                    var grp = new Group();
                    var idsArr = g["ids"] as JArray;
                    if (idsArr != null)
                    {
                        foreach (var idTok in idsArr) grp.Ids.Add((uint)(int)idTok);
                    }
                    int groupId = g.Value<int?>("groupId") ?? 0;
                    if (groupId > 0) grp.GroupId = groupId;
                    groups.Add(grp);
                }
            }
            _log.LogInfo($"SubmitSearchFromGroups: {groups.Count} groups");
            sfgReq.SubmitSelection(groups);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "SearchFromGroups", ["group_count"] = groups.Count });
        }

        private void HandleSubmitCastingManaType(PipeCommand cmd)
        {
            // CastingTimeOption_ManaTypeRequest may appear as a child of
            // CastingTimeOptionRequest. Walk children to find the mana-type
            // child request and submit the user-supplied list of colors.
            var request = FindPendingInteraction();
            CastingTimeOption_ManaTypeRequest manaReq = null;
            if (request is CastingTimeOption_ManaTypeRequest direct)
            {
                manaReq = direct;
            }
            else if (request is CastingTimeOptionRequest parent)
            {
                foreach (var child in parent.ChildRequests)
                {
                    if (child is CastingTimeOption_ManaTypeRequest m) { manaReq = m; break; }
                }
            }
            if (manaReq == null)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Pending is {request?.GetType().Name ?? "null"}, no CastingTimeOption_ManaTypeRequest child" });
                return;
            }
            // Payload: {"colors": ["White", "Blue", ...]} matching InnerRequests count.
            var colorsJson = cmd.Json["colors"] as JArray;
            var colors = new List<ManaColor>();
            if (colorsJson != null)
            {
                foreach (var cTok in colorsJson)
                {
                    if (System.Enum.TryParse<ManaColor>((string)cTok, out var color))
                        colors.Add(color);
                    else
                        colors.Add(ManaColor.None);
                }
            }
            if (colors.Count != manaReq.InnerRequests.Count)
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = $"Need {manaReq.InnerRequests.Count} colors, got {colors.Count}" });
                return;
            }
            _log.LogInfo($"SubmitCastingManaType: {string.Join(",", colors)}");
            manaReq.SubmitSelection(colors);
            lock (_interactionLock) { _lastKnownRequest = null; }
            cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "CastingManaType", ["colors"] = string.Join(",", colors) });
        }

        private void HandleAutoRespond(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request != null)
            {
                _log.LogInfo($"AutoRespond on {request.GetType().Name}");
                request.AutoRespond();
                lock (_interactionLock) { _lastKnownRequest = null; }
                cmd.SetResponse(new JObject { ["ok"] = true, ["submitted_type"] = "AutoRespond", ["request_class"] = request.GetType().Name });
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No pending interaction" });
            }
        }

        private void HandleCancelAction(PipeCommand cmd)
        {
            var request = FindPendingInteraction();
            if (request != null)
            {
                if (request.CanCancel)
                {
                    _log.LogInfo($"Cancel on {request.GetType().Name}");
                    request.Cancel();
                    lock (_interactionLock) { _lastKnownRequest = null; }
                    cmd.SetResponse(new JObject { ["ok"] = true, ["cancelled"] = true, ["request_class"] = request.GetType().Name });
                }
                else
                {
                    _log.LogInfo($"Cancel not allowed on {request.GetType().Name}, using AutoRespond");
                    request.AutoRespond();
                    lock (_interactionLock) { _lastKnownRequest = null; }
                    cmd.SetResponse(new JObject { ["ok"] = true, ["cancelled"] = false, ["request_class"] = request.GetType().Name });
                }
            }
            else
            {
                cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "No pending interaction" });
            }
        }

        internal static JObject BuildPendingRequestPayload(BaseUserRequest request)
        {
            var payload = new JObject();
            var requestType = request.Type.ToString();
            payload["requestType"] = requestType;
            payload["requestClass"] = request.GetType().Name;

            foreach (var prop in request.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (!prop.CanRead || prop.GetIndexParameters().Length != 0)
                    continue;

                if (ShouldSkipPendingRequestProperty(requestType, prop.Name))
                    continue;

                object value;
                try
                {
                    value = prop.GetValue(request, null);
                }
                catch
                {
                    continue;
                }

                if (IsNullOrEmptyValue(value) || !IsInterestingPendingRequestProperty(prop.Name, value))
                    continue;

                var token = SerializePendingRequestValue(value, 0);
                if (token != null)
                    payload[ToCamelCase(prop.Name)] = token;
            }

            return payload;
        }

        internal static JObject BuildPendingRequestDecisionContext(BaseUserRequest request, JObject requestPayload)
        {
            var requestType = request.Type.ToString();
            var context = new JObject
            {
                ["requestType"] = requestType,
                ["requestClass"] = request.GetType().Name
            };

            var mappedType = MapRequestTypeToDecisionType(requestType);
            if (!string.IsNullOrEmpty(mappedType))
                context["type"] = mappedType;

            CopyFieldIfPresent(requestPayload, context, "prompt", "promptText", "message", "messageText", "help", "helpText");
            CopyFieldIfPresent(requestPayload, context, "sourceId", "grpId", "abilityGrpId", "zoneId");
            CopyFieldIfPresent(requestPayload, context, "count", "min", "max", "minCount", "maxCount", "amount", "total");
            CopyFieldIfPresent(
                requestPayload,
                context,
                "ids",
                "options",
                "targets",
                "validTargets",
                "targetsToSelect",
                "qualifiedTargets",
                "attackers",
                "qualifiedAttackers",
                "blockers",
                "qualifiedBlockers",
                "groups",
                "counterTypes");

            if (context.Count <= 2)
                return null;

            return context;
        }

        private static JObject BuildCastingTimeOptionDecisionContext(IReadOnlyList<CastingTimeOptionEntry> entries)
        {
            var options = new JArray();
            foreach (var entry in entries)
            {
                options.Add(entry.Payload.DeepClone());
            }

            return new JObject
            {
                ["type"] = "casting_time_options",
                ["num_options"] = entries.Count,
                ["options"] = options
            };
        }

        private static JObject CreateCastingTimeOptionPayload(
            string choiceKind,
            BaseUserRequest request,
            int childIndex,
            string label,
            int? optionIndex = null,
            uint grpId = 0)
        {
            var payload = new JObject
            {
                ["actionType"] = "CastingTimeOption",
                ["choiceKind"] = choiceKind,
                ["requestClass"] = request.GetType().Name,
                ["childIndex"] = childIndex,
                ["label"] = label
            };

            if (optionIndex.HasValue)
                payload["optionIndex"] = optionIndex.Value;
            if (grpId != 0)
                payload["grpId"] = (int)grpId;
            if (request.SourceId != 0)
                payload["sourceId"] = (int)request.SourceId;

            return payload;
        }

        private static void AddManaCost(JObject payload, IEnumerable<ManaRequirement> manaCost)
        {
            if (manaCost == null)
                return;

            var costs = new JArray();
            foreach (var cost in manaCost)
            {
                costs.Add(new JObject
                {
                    ["color"] = cost.Color.ToString(),
                    ["count"] = (int)cost.Count
                });
            }

            if (costs.Count > 0)
                payload["manaCost"] = costs;
        }

        private static bool ShouldSkipPendingRequestProperty(string requestType, string propertyName)
        {
            switch (propertyName)
            {
                case "Type":
                case "CanCancel":
                case "AllowUndo":
                    return true;
            }

            if (requestType == "ActionsAvailableReq" && propertyName == "Actions")
                return true;

            if (requestType == "CastingTimeOptionsReq" && propertyName == "ChildRequests")
                return true;

            return false;
        }

        private static bool IsInterestingPendingRequestProperty(string propertyName, object value)
        {
            if (value == null)
                return false;

            var type = value.GetType();
            if (type == typeof(string))
                return !string.IsNullOrWhiteSpace((string)value);

            if (IsSimpleSerializableType(type))
                return HasInterestingPendingRequestName(propertyName);

            if (value is System.Collections.IEnumerable && !(value is string))
                return HasInterestingPendingRequestName(propertyName);

            return HasInterestingPendingRequestName(propertyName)
                || HasInterestingPendingRequestName(type.Name);
        }

        private static bool HasInterestingPendingRequestName(string name)
        {
            if (string.IsNullOrEmpty(name))
                return false;

            string lower = name.ToLowerInvariant();
            string[] keywords =
            {
                "prompt", "help", "message", "label", "text", "context",
                "target", "option", "choice", "select", "group", "search",
                "source", "zone", "attack", "block", "counter",
                "id", "count", "min", "max", "amount", "total", "value"
            };

            for (int i = 0; i < keywords.Length; i++)
            {
                if (lower.Contains(keywords[i]))
                    return true;
            }

            return false;
        }

        private static bool IsNullOrEmptyValue(object value)
        {
            if (value == null)
                return true;

            if (value is string s)
                return string.IsNullOrWhiteSpace(s);

            if (value is System.Collections.ICollection collection)
                return collection.Count == 0;

            return false;
        }

        private static JToken SerializePendingRequestValue(object value, int depth)
        {
            if (value == null)
                return null;

            if (depth > 3)
                return new JValue(value.ToString());

            if (value is JToken token)
                return token.DeepClone();

            var type = value.GetType();
            if (IsSimpleSerializableType(type))
            {
                if (type.IsEnum)
                    return new JValue(value.ToString());

                try
                {
                    return JToken.FromObject(value);
                }
                catch
                {
                    return new JValue(value.ToString());
                }
            }

            if (value is System.Collections.IDictionary dictionary)
            {
                var obj = new JObject();
                int count = 0;
                foreach (System.Collections.DictionaryEntry entry in dictionary)
                {
                    if (count++ >= 32)
                    {
                        obj["_truncated"] = true;
                        break;
                    }

                    if (entry.Key == null)
                        continue;

                    var child = SerializePendingRequestValue(entry.Value, depth + 1);
                    if (child != null)
                        obj[entry.Key.ToString()] = child;
                }

                return obj.Count > 0 ? obj : null;
            }

            if (value is System.Collections.IEnumerable enumerable && !(value is string))
            {
                var arr = new JArray();
                int count = 0;
                foreach (var item in enumerable)
                {
                    if (count++ >= 64)
                    {
                        arr.Add(new JObject { ["_truncated"] = true });
                        break;
                    }

                    var child = SerializePendingRequestValue(item, depth + 1);
                    arr.Add(child ?? JValue.CreateNull());
                }

                return arr.Count > 0 ? arr : null;
            }

            if (!string.IsNullOrEmpty(type.Namespace) && type.Namespace.StartsWith("UnityEngine", StringComparison.Ordinal))
                return new JValue(value.ToString());

            var result = new JObject();
            int propertyCount = 0;
            foreach (var prop in type.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (!prop.CanRead || prop.GetIndexParameters().Length != 0)
                    continue;

                object propValue;
                try
                {
                    propValue = prop.GetValue(value, null);
                }
                catch
                {
                    continue;
                }

                if (IsNullOrEmptyValue(propValue))
                    continue;

                if (propertyCount++ >= 24)
                {
                    result["_truncated"] = true;
                    break;
                }

                var child = SerializePendingRequestValue(propValue, depth + 1);
                if (child != null)
                    result[ToCamelCase(prop.Name)] = child;
            }

            return result.Count > 0 ? result : new JValue(value.ToString());
        }

        private static bool IsSimpleSerializableType(Type type)
        {
            if (type.IsEnum || type.IsPrimitive)
                return true;

            return type == typeof(string)
                || type == typeof(decimal)
                || type == typeof(DateTime)
                || type == typeof(Guid)
                || type == typeof(TimeSpan);
        }

        private static string ToCamelCase(string name)
        {
            if (string.IsNullOrEmpty(name) || !char.IsUpper(name[0]))
                return name;

            if (name.Length == 1)
                return char.ToLowerInvariant(name[0]).ToString();

            return char.ToLowerInvariant(name[0]) + name.Substring(1);
        }

        private static void CopyFieldIfPresent(JObject source, JObject destination, params string[] propertyNames)
        {
            if (source == null || destination == null || propertyNames == null)
                return;

            for (int i = 0; i < propertyNames.Length; i++)
            {
                var token = source[propertyNames[i]];
                if (token != null && token.Type != JTokenType.Null)
                    destination[propertyNames[i]] = token.DeepClone();
            }
        }

        private static string MapRequestTypeToDecisionType(string requestType)
        {
            switch (requestType)
            {
                case "SelectTargetsReq":
                    return "target_selection";
                case "SearchReq":
                    return "search";
                case "DistributionReq":
                    return "distribution";
                case "NumericInputReq":
                    return "numeric_input";
                case "SelectNReq":
                    return "select_n";
                case "GroupReq":
                    return "group_selection";
                case "GroupOptionReq":
                    return "modal_choice";
                case "DeclareAttackersReq":
                    return "declare_attackers";
                case "DeclareBlockersReq":
                    return "declare_blockers";
                case "PayCostsReq":
                    return "pay_costs";
                case "ChooseStartingPlayerReq":
                    return "choose_starting_player";
                case "SelectReplacementReq":
                    return "select_replacement";
                case "SelectNGroupReq":
                    return "select_n_group";
                case "SelectFromGroupsReq":
                    return "select_from_groups";
                case "SearchFromGroupsReq":
                    return "search_from_groups";
                case "SelectCountersReq":
                    return "select_counters";
                case "OrderReq":
                    return "order_triggers";
                case "GatherReq":
                    return "gather";
                default:
                    return null;
            }
        }

        private List<CastingTimeOptionEntry> BuildCastingTimeOptionEntries(CastingTimeOptionRequest request)
        {
            var entries = new List<CastingTimeOptionEntry>();

            for (int childIndex = 0; childIndex < request.ChildRequests.Count; childIndex++)
            {
                var child = request.ChildRequests[childIndex];
                switch (child)
                {
                    case CastingTimeOption_ModalRequest modalReq:
                        for (int optionIndex = 0; optionIndex < modalReq.ModalOptions.Count; optionIndex++)
                        {
                            uint grpId = modalReq.ModalOptions[optionIndex];
                            var payload = CreateCastingTimeOptionPayload(
                                "modal",
                                modalReq,
                                childIndex,
                                $"Mode {optionIndex + 1}",
                                optionIndex,
                                grpId);
                            if (modalReq.AbilityGrpId != 0)
                                payload["abilityGrpId"] = (int)modalReq.AbilityGrpId;
                            if (modalReq.Min > 0)
                                payload["min"] = (int)modalReq.Min;
                            if (modalReq.Max > 0)
                                payload["max"] = (int)modalReq.Max;
                            if (modalReq.OtherSelection != null && modalReq.OtherSelection.Count > 0)
                            {
                                var otherSelection = new JArray();
                                foreach (var selection in modalReq.OtherSelection)
                                    otherSelection.Add((int)selection);
                                payload["otherSelection"] = otherSelection;
                            }

                            entries.Add(new CastingTimeOptionEntry(
                                payload,
                                () => modalReq.SubmitModal(new[] { grpId })));
                        }
                        break;

                    case CastingTimeOption_ChooseOrCostRequest chooseReq:
                        var chooseOptions = chooseReq.Options;
                        for (int optionIndex = 0; optionIndex < chooseOptions.Count; optionIndex++)
                        {
                            uint promptId = chooseOptions[optionIndex].Key;
                            uint selectionId = chooseOptions[optionIndex].Value;
                            var payload = CreateCastingTimeOptionPayload(
                                "choose_or_cost",
                                chooseReq,
                                childIndex,
                                $"Choice {optionIndex + 1}",
                                optionIndex,
                                chooseReq.GrpId);
                            if (promptId != 0)
                                payload["promptId"] = (int)promptId;
                            payload["selection"] = (int)selectionId;
                            if (chooseReq.Min > 0)
                                payload["min"] = chooseReq.Min;
                            if (chooseReq.Max > 0)
                                payload["max"] = (int)chooseReq.Max;

                            entries.Add(new CastingTimeOptionEntry(
                                payload,
                                () => chooseReq.SubmitChoice(selectionId)));
                        }
                        break;

                    case CastingTimeOption_DoneRequest doneReq:
                        var donePayload = CreateCastingTimeOptionPayload(
                            "done",
                            doneReq,
                            childIndex,
                            "Done");
                        AddManaCost(donePayload, doneReq.ManaCost);
                        entries.Add(new CastingTimeOptionEntry(donePayload, doneReq.SubmitDone));
                        break;

                    case CastingTimeOption_TimingPermissionRequest timingReq:
                        var timingPayload = CreateCastingTimeOptionPayload(
                            "timing_permission",
                            timingReq,
                            childIndex,
                            "Timing Permission",
                            grpId: timingReq.GrpId);
                        AddManaCost(timingPayload, timingReq.ManaCost);
                        entries.Add(new CastingTimeOptionEntry(timingPayload, timingReq.SubmitFlash));
                        break;

                    case CastingTimeOption_KickerRequest kickerReq:
                        var kickerPayload = CreateCastingTimeOptionPayload(
                            "kicker",
                            kickerReq,
                            childIndex,
                            "Kicker",
                            grpId: kickerReq.GrpId);
                        AddManaCost(kickerPayload, kickerReq.ManaCost);
                        entries.Add(new CastingTimeOptionEntry(kickerPayload, kickerReq.SubmitKicked));
                        break;

                    case CastingTimeOption_AdditionalCostRequest additionalReq:
                        var additionalPayload = CreateCastingTimeOptionPayload(
                            "additional_cost",
                            additionalReq,
                            childIndex,
                            "Additional Cost",
                            grpId: additionalReq.GrpId);
                        AddManaCost(additionalPayload, additionalReq.ManaCost);
                        entries.Add(new CastingTimeOptionEntry(additionalPayload, additionalReq.SubmitAdditionalCost));
                        break;

                    case CastingTimeOption_CostKeywordRequest keywordReq:
                        var keywordPayload = CreateCastingTimeOptionPayload(
                            "cost_keyword",
                            keywordReq,
                            childIndex,
                            keywordReq.OptionType.ToString(),
                            grpId: keywordReq.GrpId);
                        entries.Add(new CastingTimeOptionEntry(keywordPayload, keywordReq.SubmitKeywordAction));
                        break;

                    case CastingTimeOption_NumericInputRequest numericReq when numericReq.Min == numericReq.Max:
                        uint numericValue = numericReq.Min;
                        var numericPayload = CreateCastingTimeOptionPayload(
                            "numeric_input",
                            numericReq,
                            childIndex,
                            $"Value {numericValue}",
                            grpId: numericReq.GrpId);
                        numericPayload["numericValue"] = (int)numericValue;
                        entries.Add(new CastingTimeOptionEntry(
                            numericPayload,
                            () => numericReq.SubmitX(numericValue)));
                        break;

                    case CastingTimeOption_NumericInputRequest variableNumericReq:
                        // Min != Max — enumerate up to MaxNumericInputEntries values so
                        // the existing entry-index protocol can pick one. Honor
                        // disallowed/even/odd filters and suggested values.
                        foreach (uint value in EnumerateNumericInputValues(variableNumericReq))
                        {
                            uint capturedValue = value;
                            var entryPayload = CreateCastingTimeOptionPayload(
                                "numeric_input",
                                variableNumericReq,
                                childIndex,
                                $"X = {capturedValue}",
                                grpId: variableNumericReq.GrpId);
                            entryPayload["numericValue"] = (int)capturedValue;
                            entryPayload["min"] = (int)variableNumericReq.Min;
                            entryPayload["max"] = (int)variableNumericReq.Max;
                            entries.Add(new CastingTimeOptionEntry(
                                entryPayload,
                                () => variableNumericReq.SubmitX(capturedValue)));
                        }
                        break;

                    case CastingTimeOption_Replicate replicateReq when replicateReq.Min == replicateReq.Max:
                        uint replicateValue = replicateReq.Min;
                        var replicatePayload = CreateCastingTimeOptionPayload(
                            "replicate",
                            replicateReq,
                            childIndex,
                            $"Replicate {replicateValue}");
                        replicatePayload["numericValue"] = (int)replicateValue;
                        entries.Add(new CastingTimeOptionEntry(
                            replicatePayload,
                            () => replicateReq.SubmitValue(replicateValue)));
                        break;

                    case CastingTimeOption_Replicate variableReplicateReq:
                        // Min != Max — enumerate replicate counts.
                        for (uint v = variableReplicateReq.Min; v <= variableReplicateReq.Max && v - variableReplicateReq.Min < MaxNumericInputEntries; v++)
                        {
                            uint capturedReplicate = v;
                            var rPayload = CreateCastingTimeOptionPayload(
                                "replicate",
                                variableReplicateReq,
                                childIndex,
                                $"Replicate {capturedReplicate}");
                            rPayload["numericValue"] = (int)capturedReplicate;
                            rPayload["min"] = (int)variableReplicateReq.Min;
                            rPayload["max"] = (int)variableReplicateReq.Max;
                            entries.Add(new CastingTimeOptionEntry(
                                rPayload,
                                () => variableReplicateReq.SubmitValue(capturedReplicate)));
                        }
                        break;

                    case CastingTimeOption_SpecializeRequest specializeReq:
                        foreach (CardColor color in specializeReq.SelectableColors)
                        {
                            CardColor capturedColor = color;
                            var sPayload = CreateCastingTimeOptionPayload(
                                "specialize",
                                specializeReq,
                                childIndex,
                                $"Specialize: {capturedColor}",
                                grpId: specializeReq.SourceAbilityId);
                            sPayload["colorName"] = capturedColor.ToString();
                            sPayload["colorValue"] = (int)capturedColor;
                            entries.Add(new CastingTimeOptionEntry(
                                sPayload,
                                () => specializeReq.SubmitSpecialization(capturedColor)));
                        }
                        break;

                    case CastingTimeOption_ManaTypeRequest manaTypeReq:
                        // Each InnerRequest picks one ManaColor. The default
                        // payload submits the per-inner DefaultIndex choice;
                        // a full multi-color picker would need a separate
                        // explicit submit handler with a Python-supplied list.
                        var defaultColors = new List<ManaColor>();
                        foreach (var inner in manaTypeReq.InnerRequests)
                        {
                            int idx = Math.Min(inner.DefaultIndex, inner.ManaColorOptions.Count - 1);
                            defaultColors.Add(inner.ManaColorOptions[Math.Max(0, idx)]);
                        }
                        var mPayload = CreateCastingTimeOptionPayload(
                            "mana_type",
                            manaTypeReq,
                            childIndex,
                            $"Mana types: {string.Join(",", defaultColors)}");
                        var colorList = new JArray();
                        foreach (var c in defaultColors) colorList.Add(c.ToString());
                        mPayload["colors"] = colorList;
                        entries.Add(new CastingTimeOptionEntry(
                            mPayload,
                            () => manaTypeReq.SubmitSelection(defaultColors)));
                        break;
                }
            }

            return entries;
        }

        // -------------------------------------------------------------------
        // resolve_grp_ids — name resolution through the client itself.
        // Dynamically-created objects (copies, modified cards) carry runtime
        // grpIds far above the catalog range; no static database can name
        // them. The client can: the card-title provider first, then live
        // game-state instances (instance TitleId -> GreLocProvider).
        // -------------------------------------------------------------------

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
        private void SetSaveDSReplayPref(bool enabled)
        {
            try
            {
                Type cachedPrefsType = null;
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        cachedPrefsType = asm.GetType("Core.Code.Utils.PlayerPrefsUtils.CachedPlayerPrefs");
                        if (cachedPrefsType != null) break;
                    }
                    catch { }
                }

                if (cachedPrefsType != null)
                {
                    var setInt = cachedPrefsType.GetMethod("SetInt",
                        BindingFlags.Public | BindingFlags.Static,
                        null, new[] { typeof(string), typeof(int) }, null);
                    if (setInt != null)
                    {
                        setInt.Invoke(null, new object[] { "SaveDSReplay", enabled ? 1 : 0 });
                        _log.LogInfo($"Set CachedPlayerPrefs SaveDSReplay = {enabled}");
                    }
                    else
                    {
                        PlayerPrefs.SetInt("SaveDSReplay", enabled ? 1 : 0);
                        _log.LogInfo($"CachedPlayerPrefs.SetInt not found, fell back to PlayerPrefs");
                    }
                }
                else
                {
                    PlayerPrefs.SetInt("SaveDSReplay", enabled ? 1 : 0);
                    _log.LogInfo($"CachedPlayerPrefs type not found, fell back to PlayerPrefs");
                }
                PlayerPrefs.Save();
            }
            catch (Exception ex)
            {
                _log.LogWarning($"SetSaveDSReplayPref error: {ex.Message}");
                PlayerPrefs.SetInt("SaveDSReplay", enabled ? 1 : 0);
                PlayerPrefs.Save();
            }
        }

        /// <summary>
        /// Sets the ReplayName preference using CachedPlayerPrefs so the cache stays in sync.
        /// </summary>
        private void SetReplayNamePref(string name)
        {
            try
            {
                Type cachedPrefsType = null;
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        cachedPrefsType = asm.GetType("Core.Code.Utils.PlayerPrefsUtils.CachedPlayerPrefs");
                        if (cachedPrefsType != null) break;
                    }
                    catch { }
                }

                if (cachedPrefsType != null)
                {
                    var setString = cachedPrefsType.GetMethod("SetString",
                        BindingFlags.Public | BindingFlags.Static,
                        null, new[] { typeof(string), typeof(string) }, null);
                    if (setString != null)
                    {
                        setString.Invoke(null, new object[] { "ReplayName", name ?? "" });
                        return;
                    }
                }
                PlayerPrefs.SetString("ReplayName", name ?? "");
                PlayerPrefs.Save();
            }
            catch (Exception ex)
            {
                _log.LogDebug($"SetReplayNamePref error: {ex.Message}");
                PlayerPrefs.SetString("ReplayName", name ?? "");
                PlayerPrefs.Save();
            }
        }

        /// <summary>
        /// Reads MDNPlayerPrefs.SaveDSReplays via reflection (it checks CachedPlayerPrefs).
        /// Falls back to raw PlayerPrefs if the type can't be found.
        /// </summary>
        private bool GetSaveDSReplayPref()
        {
            try
            {
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type mdnType = null;
                    try { mdnType = asm.GetType("MDNPlayerPrefs"); } catch { continue; }
                    if (mdnType == null) continue;

                    var prop = mdnType.GetProperty("SaveDSReplays",
                        BindingFlags.Public | BindingFlags.Static);
                    if (prop != null && prop.PropertyType == typeof(bool))
                        return (bool)prop.GetValue(null);
                }
            }
            catch { }
            return PlayerPrefs.GetInt("SaveDSReplay", 0) == 1;
        }

        /// <summary>
        /// Attempts to trigger recording on the current match by calling
        /// TimedReplayRecorder.StartMatch(MatchManager) via reflection.
        /// This is the same method MTGA calls when a match begins.
        /// The recorder's StartMatch checks MDNPlayerPrefs.SaveDSReplays internally,
        /// so the pref must be set to true before calling this.
        /// </summary>
        private bool TryStartRecordingOnCurrentMatch(object recorder)
        {
            if (recorder == null) return false;
            try
            {
                var gm = GetGameManager();
                if (gm == null) return false;
                var mm = gm.MatchManager;
                if (mm == null) return false;

                // Call StartMatch(MatchManager) via reflection
                var startMatch = recorder.GetType().GetMethod("StartMatch",
                    BindingFlags.Public | BindingFlags.Instance);
                if (startMatch != null)
                {
                    startMatch.Invoke(recorder, new object[] { mm });
                    _log.LogInfo("Called TimedReplayRecorder.StartMatch(MatchManager)");
                    return true;
                }
                else
                {
                    _log.LogDebug("StartMatch method not found on TimedReplayRecorder");
                }
            }
            catch (Exception ex)
            {
                _log.LogWarning($"TryStartRecordingOnCurrentMatch error: {ex.Message}");
            }
            return false;
        }

        private void HandleEnableReplay(PipeCommand cmd)
        {
            try
            {
                string prefix = cmd.Json.Value<string>("replay_name");

                // Set prefs via CachedPlayerPrefs so in-memory cache + PlayerPrefs stay in sync.
                // MDNPlayerPrefs.SaveDSReplays reads from CachedPlayerPrefs, so if we only
                // write to raw PlayerPrefs the recorder's StartMatch guard would still see false.
                SetSaveDSReplayPref(true);

                if (!string.IsNullOrEmpty(prefix))
                    SetReplayNamePref(prefix);

                var resp = new JObject
                {
                    ["ok"] = true,
                    ["replay_folder"] = GetReplayFolder(),
                    ["prefs_enabled"] = GetSaveDSReplayPref(),
                };

                // Try to start recording on the current live match
                var recorder = FindReplayRecorder();
                if (recorder != null)
                {
                    bool alreadyRecording = IsRecorderRecording(recorder);
                    resp["recorder_found"] = true;
                    resp["recorder_type"] = recorder.GetType().Name;

                    if (alreadyRecording)
                    {
                        resp["recording"] = true;
                        resp["replay_file"] = GetRecorderFilePath(recorder);
                        _log.LogInfo("Replay recorder already recording");
                    }
                    else
                    {
                        // Call TimedReplayRecorder.StartMatch(MatchManager) -- the same
                        // entry point MTGA uses when Matchmaking fires MatchManagerInitialized.
                        // StartMatch internally checks MDNPlayerPrefs.SaveDSReplays (set above).
                        bool started = TryStartRecordingOnCurrentMatch(recorder);
                        resp["recording"] = started && IsRecorderRecording(recorder);
                        resp["replay_file"] = GetRecorderFilePath(recorder);
                        if (!started)
                            resp["note"] = "Pref set; recording will begin on next match start";
                    }
                }
                else
                {
                    resp["recorder_found"] = false;
                    resp["recording"] = false;
                    resp["note"] = "TimedReplayRecorder not found (PAPA not ready?); pref set for next match";
                    _log.LogInfo("No TimedReplayRecorder found -- prefs set for next match");
                }

                _log.LogInfo($"Replay recording enabled (prefix: {prefix ?? "default"})");
                cmd.SetResponse(resp);
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
                // Disable the pref so future matches won't auto-record
                SetSaveDSReplayPref(false);

                // Stop any active recording by calling CompleteMatch() on the recorder.
                // CompleteMatch is private, so we use reflection.
                var recorder = FindReplayRecorder();
                bool wasStopped = false;
                if (recorder != null)
                {
                    try
                    {
                        var completeMatch = recorder.GetType().GetMethod("CompleteMatch",
                            BindingFlags.NonPublic | BindingFlags.Instance);
                        if (completeMatch != null)
                        {
                            completeMatch.Invoke(recorder, null);
                            wasStopped = true;
                            _log.LogInfo("Called CompleteMatch() on TimedReplayRecorder");
                        }
                    }
                    catch (Exception ex)
                    {
                        _log.LogDebug($"CompleteMatch call failed: {ex.Message}");
                    }
                }

                _log.LogInfo("Replay recording disabled");
                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["enabled"] = false,
                    ["recording_stopped"] = wasStopped,
                    ["prefs_enabled"] = GetSaveDSReplayPref(),
                });
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
                string folder = GetReplayFolder();
                var recorder = FindReplayRecorder();
                bool recording = recorder != null && IsRecorderRecording(recorder);

                var resp = new JObject
                {
                    ["ok"] = true,
                    ["recording"] = recording,
                    ["recorder_found"] = recorder != null,
                    ["recorder_type"] = recorder?.GetType().Name,
                    ["replay_folder"] = folder,
                    ["replay_file"] = recording ? GetRecorderFilePath(recorder) : null,
                    ["prefs_enabled"] = GetSaveDSReplayPref(),
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
                            Array.Sort(files);
                            resp["latest_replay"] = System.IO.Path.GetFileName(files[files.Length - 1]);
                        }
                    }
                }
                catch { }

                // Dump recorder type info for debugging
                if (recorder != null)
                {
                    var methods = new JArray();
                    var fields = new JArray();
                    var flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
                    foreach (var m in recorder.GetType().GetMethods(flags))
                    {
                        if (m.DeclaringType == recorder.GetType())
                            methods.Add(m.Name);
                    }
                    foreach (var f in recorder.GetType().GetFields(flags))
                    {
                        if (f.DeclaringType == recorder.GetType())
                            fields.Add($"{f.Name} ({f.FieldType.Name})");
                    }
                    resp["_debug_methods"] = methods;
                    resp["_debug_fields"] = fields;
                }

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

        // -------------------------------------------------------------------
        // get_card_positions — return on-screen rectangles for every visible
        // DuelScene_CDC (card GameObject) in the current match.
        //
        // Used by the Python desktop match overlay to highlight the suggested
        // card or action directly on the game. This is the ground-truth
        // replacement for screen_mapper heuristics.
        // -------------------------------------------------------------------

        private void HandleGetCardPositions(PipeCommand cmd)
        {
            try
            {
                var gm = GetGameManager();
                if (gm == null)
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "GameManager not found" });
                    return;
                }

                var cam = gm.MainCamera;
                if (cam == null)
                {
                    cmd.SetResponse(new JObject { ["ok"] = false, ["error"] = "MainCamera not available" });
                    return;
                }

                // Unity Screen.height is the game-render height (internal
                // resolution). Python overlay uses MTGA window client-area
                // bounds. Both should be the same in windowed mode.
                int screenW = Screen.width;
                int screenH = Screen.height;

                var cards = new JArray();
                // DuelScene_CDC extends BASE_CDC : MonoBehaviour — every
                // visible in-match card inherits from it.
                var cardObjs = UnityEngine.Object.FindObjectsOfType<DuelScene_CDC>();
                foreach (var card in cardObjs)
                {
                    if (card == null) continue;

                    bool visible = false;
                    try { visible = card.IsVisible; } catch { }
                    if (!visible) continue;

                    uint instanceId = 0;
                    string zone = "";
                    uint grpId = 0;
                    try
                    {
                        instanceId = card.InstanceId;
                        if (card.Model != null)
                        {
                            zone = card.Model.ZoneType.ToString();
                            try { grpId = (uint)card.Model.GrpId; } catch { }
                        }
                    }
                    catch { }
                    if (instanceId == 0) continue;

                    // Project the card's bounds to screen space. Use the
                    // 8 corners of the collider AABB and take min/max to
                    // get a tight screen-space rectangle that accounts for
                    // perspective foreshortening.
                    float screenMinX = float.MaxValue;
                    float screenMinY = float.MaxValue;
                    float screenMaxX = float.MinValue;
                    float screenMaxY = float.MinValue;
                    bool anyFront = false;

                    if (card.Collider != null)
                    {
                        var b = card.Collider.bounds;
                        Vector3 ext = b.extents;
                        for (int dx = -1; dx <= 1; dx += 2)
                        for (int dy = -1; dy <= 1; dy += 2)
                        for (int dz = -1; dz <= 1; dz += 2)
                        {
                            var corner = new Vector3(
                                b.center.x + dx * ext.x,
                                b.center.y + dy * ext.y,
                                b.center.z + dz * ext.z);
                            var sp = cam.WorldToScreenPoint(corner);
                            if (sp.z < 0) continue; // behind camera
                            anyFront = true;
                            if (sp.x < screenMinX) screenMinX = sp.x;
                            if (sp.y < screenMinY) screenMinY = sp.y;
                            if (sp.x > screenMaxX) screenMaxX = sp.x;
                            if (sp.y > screenMaxY) screenMaxY = sp.y;
                        }
                    }
                    else if (card.Root != null)
                    {
                        var sp = cam.WorldToScreenPoint(card.Root.position);
                        if (sp.z < 0) continue;
                        anyFront = true;
                        // Fallback: fixed-size rect around the card center
                        float halfW = 60f;
                        float halfH = 84f;
                        screenMinX = sp.x - halfW;
                        screenMinY = sp.y - halfH;
                        screenMaxX = sp.x + halfW;
                        screenMaxY = sp.y + halfH;
                    }
                    else
                    {
                        continue;
                    }

                    if (!anyFront) continue;

                    // Unity uses BOTTOM-LEFT origin for screen coords.
                    // Python overlays use TOP-LEFT origin (Windows convention),
                    // so flip Y. Also clamp to [0, screenW/H] to avoid NaN.
                    float pxLeft = Mathf.Clamp(screenMinX, 0f, screenW);
                    float pxRight = Mathf.Clamp(screenMaxX, 0f, screenW);
                    float pxBottom = Mathf.Clamp(screenMinY, 0f, screenH);
                    float pxTop = Mathf.Clamp(screenMaxY, 0f, screenH);

                    // Flip Y
                    float flippedTop = screenH - pxTop;
                    float flippedBottom = screenH - pxBottom;

                    float rectX = pxLeft;
                    float rectY = flippedTop;
                    float rectW = Mathf.Max(0f, pxRight - pxLeft);
                    float rectH = Mathf.Max(0f, flippedBottom - flippedTop);

                    var entry = new JObject
                    {
                        ["instance_id"] = instanceId,
                        ["grp_id"] = grpId,
                        ["zone"] = zone,
                        ["x"] = Mathf.RoundToInt(rectX),
                        ["y"] = Mathf.RoundToInt(rectY),
                        ["w"] = Mathf.RoundToInt(rectW),
                        ["h"] = Mathf.RoundToInt(rectH),
                        ["nx"] = screenW > 0 ? rectX / screenW : 0f,
                        ["ny"] = screenH > 0 ? rectY / screenH : 0f,
                        ["nw"] = screenW > 0 ? rectW / screenW : 0f,
                        ["nh"] = screenH > 0 ? rectH / screenH : 0f,
                    };
                    cards.Add(entry);
                }

                cmd.SetResponse(new JObject
                {
                    ["ok"] = true,
                    ["screen_w"] = screenW,
                    ["screen_h"] = screenH,
                    ["count"] = cards.Count,
                    ["cards"] = cards,
                });
            }
            catch (Exception e)
            {
                _log.LogError($"get_card_positions failed: {e}");
                cmd.SetResponse(new JObject
                {
                    ["ok"] = false,
                    ["error"] = $"Exception: {e.Message}"
                });
            }
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
                ["error"] = "Command timed out waiting for Unity main thread"
            };
        }
    }

    internal static class PluginInfo
    {
        public const string GUID = "com.mtgacoach.grebridge";
        public const string Name = "MtgaCoach GRE Bridge";
        public const string Version = "0.6.3";
    }
}
