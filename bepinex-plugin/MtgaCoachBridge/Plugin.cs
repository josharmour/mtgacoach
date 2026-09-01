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
    public partial class Plugin : BaseUnityPlugin
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
        // and the bridge protocol is per-entry-index â€” too many entries make
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
            // GameObject â€” which owns this Plugin â€” gets destroyed on the
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
            _log?.LogInfo("Plugin OnDestroy â€” pipe thread + persistent host continue");
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


    }

    internal static class PluginInfo
    {
        public const string GUID = "com.mtgacoach.grebridge";
        public const string Name = "MtgaCoach GRE Bridge";
        public const string Version = "0.6.3";
    }

}
