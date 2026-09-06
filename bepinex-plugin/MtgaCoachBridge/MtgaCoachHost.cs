using System;
using System.Collections.Concurrent;
using System.Threading;
using BepInEx.Logging;
using UnityEngine;

namespace MtgaCoachBridge
{
    /// <summary>
    /// Persistent main-thread executor that owns the Unity context,
    /// the pending-command queue, and the GameManager cache.
    ///
    /// Why a separate host: BepInEx 5 attaches plugins to a manager GameObject
    /// whose lifecycle MTGA does not preserve across scene transitions —
    /// Plugin.OnDestroy fires on the first scene change, Update() stops, and
    /// FindObjectOfType lookups posted via the captured SynchronizationContext
    /// start returning null even when GameManager exists. The host lives on
    /// its own root-level GameObject (HideAndDontSave + DontDestroyOnLoad),
    /// independent of BepInEx's manager, and self-heals if it is ever
    /// destroyed.
    /// </summary>
    internal sealed class MtgaCoachHost : MonoBehaviour
    {
        public static MtgaCoachHost Instance { get; private set; }

        public ConcurrentQueue<PipeCommand> PendingCommands { get; } =
            new ConcurrentQueue<PipeCommand>();
        public SynchronizationContext UnityContext { get; private set; }
        public int MainThreadId { get; private set; }

        public ManualLogSource Log;
        public Action<PipeCommand> CommandExecutor;

        private GameManager _cachedGameManager;
        private float _lastGameManagerLookup;
        private int _lastLoggedGameManagerId;
        private static volatile bool _shuttingDown;

        public static MtgaCoachHost CreateOrFind(ManualLogSource log, Action<PipeCommand> executor)
        {
            if (Instance != null)
                return Instance;

            var go = new GameObject("MtgaCoachHost");
            go.hideFlags = HideFlags.HideAndDontSave;
            DontDestroyOnLoad(go);

            var host = go.AddComponent<MtgaCoachHost>();
            host.Log = log;
            host.CommandExecutor = executor;
            Instance = host;
            log?.LogInfo("MtgaCoachHost: created persistent host (HideAndDontSave, DDOL)");
            return host;
        }

        private void Awake()
        {
            UnityContext = SynchronizationContext.Current;
            MainThreadId = Thread.CurrentThread.ManagedThreadId;
            Log?.LogInfo(
                $"MtgaCoachHost.Awake: thread={MainThreadId}, syncContext=" +
                (UnityContext != null ? "captured" : "MISSING")
            );
        }

        private void OnApplicationQuit()
        {
            _shuttingDown = true;
        }

        private void OnDestroy()
        {
            if (_shuttingDown)
                return;

            Log?.LogWarning("MtgaCoachHost.OnDestroy fired — recreating host on next frame");
            Instance = null;
            try
            {
                CreateOrFind(Log, CommandExecutor);
            }
            catch (Exception ex)
            {
                Log?.LogError($"MtgaCoachHost: failed to recreate after OnDestroy: {ex}");
            }
        }

        private void Update()
        {
            while (PendingCommands.TryDequeue(out var cmd))
            {
                try
                {
                    CommandExecutor?.Invoke(cmd);
                }
                catch (Exception ex)
                {
                    Log?.LogError($"MtgaCoachHost: command exec error: {ex}");
                    cmd.SetResponse(new Newtonsoft.Json.Linq.JObject
                    {
                        ["ok"] = false,
                        ["error"] = ex.Message
                    });
                }
            }

            float now = Time.unscaledTime;
            if (now - _lastGameManagerLookup >= 1f)
            {
                _lastGameManagerLookup = now;
                var gm = FindObjectOfType<GameManager>();
                int gmId = gm != null ? gm.GetInstanceID() : 0;
                if (gmId != _lastLoggedGameManagerId)
                {
                    if (gm != null)
                        Log?.LogInfo($"MtgaCoachHost: GameManager acquired (id={gmId})");
                    else if (_lastLoggedGameManagerId != 0)
                        Log?.LogInfo("MtgaCoachHost: GameManager lost");
                    _lastLoggedGameManagerId = gmId;
                }
                _cachedGameManager = gm;
            }
        }

        public GameManager GetGameManager()
        {
            return _cachedGameManager;
        }
    }
}
