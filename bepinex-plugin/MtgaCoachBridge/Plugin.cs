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
        private SynchronizationContext _unityContext;
        private int _mainThreadId;

        private BaseUserRequest _lastKnownRequest;
        private readonly object _interactionLock = new object();

        // Cached reference to GameManager (only valid on main thread)
        private GameManager _cachedGameManager;
        private float _lastGameManagerLookup;

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

        private void Awake()
        {
            _log = Logger;
            _log.LogInfo($"MtgaCoachBridge v{PluginInfo.Version} loaded");
            DontDestroyOnLoad(gameObject);
            _unityContext = SynchronizationContext.Current;
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
            _log.LogInfo(
                _unityContext != null
                    ? $"Captured Unity synchronization context on thread {_mainThreadId}"
                    : $"Unity synchronization context unavailable on thread {_mainThreadId}; falling back to Update() dispatch"
            );

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
            _log.LogInfo("OnDestroy called — pipe thread continues and main-thread dispatch uses the captured synchronization context");
        }

        private void Update()
        {
            DrainPendingCommands();
        }

        private void DrainPendingCommands()
        {
            while (_pendingCommands.TryDequeue(out var cmd))
            {
                ExecutePipeCommand(cmd);
            }
        }

        private void ExecutePipeCommand(PipeCommand cmd)
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

        private JObject DispatchCommandToUnityThread(PipeCommand cmd, int timeoutMs)
        {
            if (Thread.CurrentThread.ManagedThreadId == _mainThreadId)
            {
                ExecutePipeCommand(cmd);
                return cmd.WaitForResponse(timeoutMs);
            }

            var unityContext = _unityContext;
            if (unityContext != null)
            {
                unityContext.Post(_ => ExecutePipeCommand(cmd), null);
            }
            else
            {
                _pendingCommands.Enqueue(cmd);
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
            using var writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, leaveOpen: true)
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
                    var response = DispatchCommandToUnityThread(cmd, 5000);
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
                ["request_class"] = request.GetType().Name,
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

        private static JObject BuildPendingRequestPayload(BaseUserRequest request)
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

        private static JObject BuildPendingRequestDecisionContext(BaseUserRequest request, JObject requestPayload)
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
                }
            }

            return entries;
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
                ["error"] = "Command timed out waiting for Unity main thread"
            };
        }
    }

    internal static class PluginInfo
    {
        public const string GUID = "com.mtgacoach.grebridge";
        public const string Name = "MtgaCoach GRE Bridge";
        public const string Version = "0.5.0";
    }
}
