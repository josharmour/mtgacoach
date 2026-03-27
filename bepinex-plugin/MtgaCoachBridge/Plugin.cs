using System;
using System.Collections.Concurrent;
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

        private void Awake()
        {
            _log = Logger;
            _log.LogInfo($"MtgaCoachBridge v{PluginInfo.Version} loaded");

            _running = true;
            _pipeThread = new Thread(PipeServerLoop)
            {
                IsBackground = true,
                Name = "MtgaCoachBridge-Pipe"
            };
            _pipeThread.Start();
        }

        private void OnDestroy()
        {
            _running = false;
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
        // Named pipe server
        // -------------------------------------------------------------------

        private void PipeServerLoop()
        {
            while (_running)
            {
                NamedPipeServerStream pipe = null;
                try
                {
                    pipe = new NamedPipeServerStream(
                        "mtgacoach_gre",
                        PipeDirection.InOut,
                        1,
                        PipeTransmissionMode.Byte,
                        PipeOptions.Asynchronous
                    );

                    _log.LogInfo("Pipe server waiting for connection on \\\\.\\pipe\\mtgacoach_gre");
                    pipe.WaitForConnection();
                    _log.LogInfo("Pipe client connected");

                    HandleClient(pipe);
                }
                catch (Exception ex)
                {
                    if (_running)
                        _log.LogWarning($"Pipe error: {ex.Message}");
                }
                finally
                {
                    try { pipe?.Dispose(); } catch { }
                }

                if (_running)
                    Thread.Sleep(500);
            }
        }

        private void HandleClient(NamedPipeServerStream pipe)
        {
            using var reader = new StreamReader(pipe, Encoding.UTF8, false, 4096, leaveOpen: true);
            using var writer = new StreamWriter(pipe, Encoding.UTF8, 4096, leaveOpen: true)
            {
                AutoFlush = true
            };

            while (_running && pipe.IsConnected)
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

                default:
                    cmd.SetResponse(new JObject
                    {
                        ["ok"] = false,
                        ["error"] = $"Unknown action: {action}"
                    });
                    break;
            }
        }

        /// <summary>
        /// Find the current pending interaction via GameManager (MonoBehaviour)
        /// → WorkflowController → current workflow → Request property.
        /// </summary>
        private BaseUserRequest FindPendingInteraction()
        {
            try
            {
                // GameManager is a MonoBehaviour, so FindObjectOfType works
                var gameManager = FindObjectOfType<GameManager>();
                if (gameManager == null)
                {
                    _log.LogDebug("GameManager not found in scene");
                    return null;
                }

                // GameManager.WorkflowController → current workflow
                var wfc = gameManager.WorkflowController;
                if (wfc == null)
                {
                    _log.LogDebug("WorkflowController is null");
                    return null;
                }

                // Try current workflow first, then pending
                // WorkflowController.CurrentWorkflow returns a WorkflowBase
                // which has a BaseRequest property returning BaseUserRequest
                object workflow = wfc.CurrentWorkflow;
                if (workflow == null)
                {
                    // Try pending workflow via reflection
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

                // WorkflowBase has BaseRequest property → BaseUserRequest
                var reqProp = workflow.GetType().GetProperty("BaseRequest",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (reqProp == null)
                {
                    // Fallback: try "Request" (on generic WorkflowBase<T>)
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

                // Fallback: try MatchManager → reflection for pending interaction
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

        /// <summary>
        /// Serialize a GRE Action to JSON. Avoids directly iterating protobuf
        /// RepeatedField by using count-based indexing.
        /// </summary>
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

            if (action.AutoTapSolution != null)
                obj["hasAutoTap"] = true;

            return obj;
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
        public const string Version = "0.1.0";
    }
}
