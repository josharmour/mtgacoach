using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using BepInEx.Logging;
using GreClient.Rules;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using Wotc.Mtga.Cards.Database;
using HarmonyLib;

namespace MtgaCoachBridge
{
    // Custom IHeadlessClientStrategy that forwards decision requests to Python
    // over the mtgacoach_botbattle_v2 named pipe.
    internal class BridgeStrategy : IHeadlessClientStrategy
    {
        private readonly IHeadlessClientStrategy _inner;
        private readonly string _label;
        private readonly ManualLogSource _log;
        private int _requestCount;

        public BridgeStrategy(string label, ManualLogSource log)
        {
            _label = label ?? "bridge";
            _log = log;
            // GoldfishStrategy in the ILSpy output is synthetic — the real
            // SharedClientCore.dll exports RequestHandlerStrategy +
            // GoldfishRequestHandlerFactory. Use the factory directly.
            _inner = new RequestHandlerStrategy(new GoldfishRequestHandlerFactory());
        }

        public int RequestCount => _requestCount;

        public void HandleRequest(BaseUserRequest request)
        {
            _requestCount++;
            var t = request != null ? request.GetType().Name : "<null>";
            _log?.LogInfo($"[Bridge:{_label}] #{_requestCount} HandleRequest: {t}");

            if (request == null) return;

            try
            {
                // Serialize the request using the exposed static methods of Plugin
                JObject payload = Plugin.Instance != null ? Plugin.BuildPendingRequestPayload(request) : new JObject();
                JObject context = Plugin.Instance != null ? Plugin.BuildPendingRequestDecisionContext(request, payload) : new JObject();

                // Fetch the game state JSON using the existing pipe command handler
                JObject gameState = null;
                if (Plugin.Instance != null)
                {
                    var stateCmd = new PipeCommand(new JObject { ["action"] = "get_game_state" });
                    Plugin.Instance.ExecutePipeCommand(stateCmd);
                    var stateResult = stateCmd.WaitForResponse(2000);
                    if (stateResult != null && stateResult.Value<bool>("ok"))
                    {
                        gameState = stateResult;
                    }
                }

                // Build decision request payload
                JObject msg = new JObject
                {
                    ["seat"] = _label, // "local" or "opp"
                    ["request_type"] = t,
                    ["payload"] = payload,
                    ["context"] = context,
                    ["game_state"] = gameState
                };

                // Send to Python over the bot battle pipe
                string responseStr = QueryPythonBotBattlePipe(msg.ToString(Formatting.None));

                if (!string.IsNullOrEmpty(responseStr))
                {
                    JObject responseJson = JObject.Parse(responseStr);
                    _log?.LogInfo($"[Bridge:{_label}] Python chosen action: {responseJson.ToString(Formatting.None)}");

                    // Set last known request so the submit handler finds it
                    Plugin.Instance?.SetLastKnownRequest(request);

                    // Execute the chosen submit command using the existing Plugin commands
                    var cmd = new PipeCommand(responseJson);
                    Plugin.Instance?.ExecutePipeCommand(cmd);
                    var result = cmd.WaitForResponse(5000);
                    _log?.LogInfo($"[Bridge:{_label}] Submit outcome: {result.ToString(Formatting.None)}");
                }
                else
                {
                    _log?.LogWarning($"[Bridge:{_label}] Empty/null response from Python, falling back to inner strategy (Goldfish)");
                    _inner.HandleRequest(request);
                }
            }
            catch (Exception ex)
            {
                _log?.LogError($"[Bridge:{_label}] HandleRequest failed: {ex.Message}. Falling back to inner strategy (Goldfish)");
                _log?.LogError(ex.StackTrace);
                _inner.HandleRequest(request);
            }
        }

        private string QueryPythonBotBattlePipe(string requestJson)
        {
            using (var client = new TcpClient())
            {
                try
                {
                    var result = client.BeginConnect("127.0.0.1", 44223, null, null);
                    bool success = result.AsyncWaitHandle.WaitOne(5000); // 5 seconds timeout
                    if (!success)
                    {
                        throw new TimeoutException("BotBattle TCP connection timed out");
                    }
                    client.EndConnect(result);

                    using (var stream = client.GetStream())
                    using (var reader = new StreamReader(stream, Encoding.UTF8))
                    using (var writer = new StreamWriter(stream, new UTF8Encoding(false)))
                    {
                        writer.WriteLine(requestJson);
                        writer.Flush();
                        return reader.ReadLine();
                    }
                }
                catch (Exception ex)
                {
                    _log?.LogWarning($"[Bridge:{_label}] TCP connection failed: {ex.Message}");
                    return null;
                }
            }
        }

        public void SetGameState(MtgGameState state)
        {
            _inner.SetGameState(state);
        }
    }

    internal static class BotBattleBridge
    {
        // Latest run state — exposed via bot_battle_status pipe command. The
        // start_bot_battle handler writes here from the Unity main thread and
        // the pipe-thread handler reads (locked).
        private static readonly object _stateLock = new object();
        private static string _lastError;
        private static bool _running;
        private static int _matchesRequested;
        private static int _matchesCompleted;
        private static BridgeStrategy _localStrategy;
        private static BridgeStrategy _opponentStrategy;

        public static bool IsRunning
        {
            get
            {
                lock (_stateLock)
                {
                    return _running;
                }
            }
        }

        public static void OnMatchCompleted()
        {
            lock (_stateLock)
            {
                _matchesCompleted++;
                Plugin._log?.LogInfo($"[BotBattleBridge] Match completed! Count: {_matchesCompleted}/{_matchesRequested}");
                if (_matchesCompleted >= _matchesRequested)
                {
                    _running = false;
                }
            }
        }

        public static JObject GetStatus()
        {
            lock (_stateLock)
            {
                return new JObject
                {
                    ["running"] = _running,
                    ["matches_requested"] = _matchesRequested,
                    ["matches_completed"] = _matchesCompleted,
                    ["local_request_count"] = _localStrategy != null ? _localStrategy.RequestCount : 0,
                    ["opponent_request_count"] = _opponentStrategy != null ? _opponentStrategy.RequestCount : 0,
                    ["last_error"] = _lastError ?? string.Empty,
                };
            }
        }

        public static JObject Start(JObject cfgJson, ManualLogSource log)
        {
            try
            {
                int matches = cfgJson.Value<int?>("matches") ?? 1;
                string sets = cfgJson.Value<string>("sets") ?? string.Empty; // e.g. "EOE,TDM"

                lock (_stateLock)
                {
                    if (_running)
                    {
                        return new JObject { ["ok"] = false, ["error"] = "bot battle already running" };
                    }
                    _running = true;
                    _lastError = null;
                    _matchesRequested = matches;
                    _matchesCompleted = 0;
                }

                // Build a random deck for each side. We need a CardDatabase to
                // resolve printings; the existing dev config loads one via
                // BotBattleConfigView.GetLocalCardDatabase but we need to do
                // the equivalent from BepInEx. PAPA owns the live db.
                var papa = UnityEngine.Object.FindObjectOfType<PAPA>();
                if (papa == null)
                {
                    return Fail(log, "PAPA not found — is MTGA past the title screen?");
                }
                var cardDb = papa.CardDatabase;
                if (cardDb == null)
                {
                    return Fail(log, "PAPA.CardDatabase is null — db not loaded yet?");
                }

                // Ensure we are fully loaded past the startup sequence to prevent it from aborting our scene load
                bool isHomeLoaded = false;
                for (int i = 0; i < UnityEngine.SceneManagement.SceneManager.sceneCount; i++)
                {
                    string sname = UnityEngine.SceneManagement.SceneManager.GetSceneAt(i).name;
                    if (sname == "HomePage" || sname == "MainNavigation")
                    {
                        isHomeLoaded = true;
                        break;
                    }
                }
                if (!isHomeLoaded)
                {
                    return Fail(log, "MTGA is still in the startup/login sequence — wait until you see the Home screen!");
                }

                List<uint> localDeck;
                List<uint> opponentDeck;
                try
                {
                    localDeck = BotBattleConfig_DeckTest.GenerateRandomDeckFromSets(cardDb.DatabaseUtilities, sets);
                    opponentDeck = BotBattleConfig_DeckTest.GenerateRandomDeckFromSets(cardDb.DatabaseUtilities, sets);
                }
                catch (Exception ex)
                {
                    return Fail(log, $"deck generation failed: {ex.Message}");
                }
                if (localDeck == null || localDeck.Count == 0 || opponentDeck == null || opponentDeck.Count == 0)
                {
                    return Fail(log, $"empty deck (sets='{sets}'); try a known set code");
                }

                var dsConfig = new BotBattleDSConfig
                {
                    SessionType = BotBattleSessionType.DeckTest,
                    MatchesToPlay = matches,
                    LocalPlayerStrategy = BotBattleStrategyType.Goldfish,
                    OpponentStrategy = BotBattleStrategyType.Goldfish,
                    LocalPlayerCardsToTest = new List<List<uint>> { localDeck, opponentDeck },
                    OpponentCardsToTest = new List<List<uint>> { opponentDeck },
                };

                log?.LogInfo($"[BotBattleBridge] dispatching BotBattleScene.Load: matches={matches} sets='{sets}' localDeck={localDeck.Count} oppDeck={opponentDeck.Count}");

                // Create a persistent coordinator GameObject to bypass the missing BotBattleScene asset
                var go = new GameObject("BotBattleBridgeCoordinator");
                UnityEngine.Object.DontDestroyOnLoad(go);
                var comp = go.AddComponent<BotBattleScene>();
                
                var papaField = typeof(BotBattleScene).GetField("_papa", BindingFlags.NonPublic | BindingFlags.Instance);
                if (papaField != null)
                {
                    papaField.SetValue(comp, papa);
                }
                var cardDbField = typeof(BotBattleScene).GetField("_cardDatabase", BindingFlags.NonPublic | BindingFlags.Instance);
                if (cardDbField != null)
                {
                    cardDbField.SetValue(comp, cardDb);
                }

                var enqueueMethod = typeof(BotBattleScene).GetMethod("EnqueueTests", BindingFlags.NonPublic | BindingFlags.Instance);
                if (enqueueMethod != null)
                {
                    enqueueMethod.Invoke(comp, new object[] { new BotBattleDSConfig[] { dsConfig } });
                }
                else
                {
                    log?.LogError("[BotBattleBridge] EnqueueTests method not found via reflection!");
                }

                // Defer the strategy hot-swap one frame: BotBattleScene needs
                // its scene to load + EnqueueTests to run before _testQueue
                // contains anything we can mutate. The host's Update tick is
                // the natural cadence.
                MtgaCoachHost.Instance?.StartCoroutine(SwapStrategiesNextFrames(log));

                return new JObject
                {
                    ["ok"] = true,
                    ["matches"] = matches,
                    ["local_deck_size"] = localDeck.Count,
                    ["opponent_deck_size"] = opponentDeck.Count,
                };
            }
            catch (Exception ex)
            {
                log?.LogError($"[BotBattleBridge] Start failed: {ex}");
                return Fail(log, $"unhandled: {ex.InnerException?.Message ?? ex.Message}");
            }
        }

        private static JObject Fail(ManualLogSource log, string msg)
        {
            log?.LogWarning($"[BotBattleBridge] {msg}");
            lock (_stateLock)
            {
                _lastError = msg;
                _running = false;
            }
            return new JObject { ["ok"] = false, ["error"] = msg };
        }

        // Yield-based coroutine: poll BotBattleScene for the test queue and
        // swap LocalPlayerStrategy / OpponentStrategy on the dequeued test.
        // _testQueue and _currentTest are private — reach via reflection.
        private static System.Collections.IEnumerator SwapStrategiesNextFrames(ManualLogSource log)
        {
            const float maxWaitSec = 30f;
            float elapsed = 0f;
            BotBattleScene scene = null;
            while (elapsed < maxWaitSec)
            {
                yield return null;
                elapsed += Time.unscaledDeltaTime;
                scene = UnityEngine.Object.FindObjectOfType<BotBattleScene>();
                if (scene != null) break;
            }
            if (scene == null)
            {
                Fail(log, "BotBattleScene didn't appear within 30s — scene transition blocked?");
                yield break;
            }

            // Wait for _currentTest to be assigned (RunTest dequeues + sets)
            var sceneType = typeof(BotBattleScene);
            var currentTestField = sceneType.GetField("_currentTest", BindingFlags.NonPublic | BindingFlags.Instance);
            if (currentTestField == null)
            {
                Fail(log, "_currentTest field not found via reflection");
                yield break;
            }

            float swapWait = 0f;
            BotBattleTest currentTest = null;
            while (swapWait < maxWaitSec)
            {
                yield return null;
                swapWait += Time.unscaledDeltaTime;
                currentTest = currentTestField.GetValue(scene) as BotBattleTest;
                if (currentTest != null) break;
            }
            if (currentTest == null)
            {
                Fail(log, "_currentTest stayed null — test never started");
                yield break;
            }

            var local = new BridgeStrategy("local", log);
            var opp = new BridgeStrategy("opp", log);
            currentTest.LocalPlayerStrategy = local;
            currentTest.OpponentStrategy = opp;
            lock (_stateLock)
            {
                _localStrategy = local;
                _opponentStrategy = opp;
            }
            log?.LogInfo("[BotBattleBridge] strategies swapped on _currentTest");
        }
    }

    [HarmonyPatch(typeof(BotBattleScene), "Awake")]
    internal static class BotBattleScene_Awake_Patch
    {
        [HarmonyPrefix]
        private static bool Prefix(BotBattleScene __instance)
        {
            Plugin._log?.LogInfo("[HarmonyPatch] BotBattleScene.Awake prefix called! Skipping original Awake to prevent scene load.");
            
            // Initialize lists and queues
            var exceptionsField = typeof(BotBattleScene).GetField("_exceptions", BindingFlags.NonPublic | BindingFlags.Instance);
            if (exceptionsField != null && exceptionsField.GetValue(__instance) == null)
            {
                exceptionsField.SetValue(__instance, new List<Exception>());
            }
            var errorsField = typeof(BotBattleScene).GetField("_errors", BindingFlags.NonPublic | BindingFlags.Instance);
            if (errorsField != null && errorsField.GetValue(__instance) == null)
            {
                errorsField.SetValue(__instance, new List<string>());
            }
            var assertsField = typeof(BotBattleScene).GetField("_asserts", BindingFlags.NonPublic | BindingFlags.Instance);
            if (assertsField != null && assertsField.GetValue(__instance) == null)
            {
                assertsField.SetValue(__instance, new List<string>());
            }
            var queueField = typeof(BotBattleScene).GetField("_testQueue", BindingFlags.NonPublic | BindingFlags.Instance);
            if (queueField != null && queueField.GetValue(__instance) == null)
            {
                queueField.SetValue(__instance, new Queue<BotBattleTest>());
            }
            var loadingField = typeof(BotBattleScene).GetField("_loadingScene", BindingFlags.NonPublic | BindingFlags.Static);
            if (loadingField != null)
            {
                loadingField.SetValue(null, false);
            }

            return false; // Skip original Awake
        }
    }

    [HarmonyPatch(typeof(BotBattleScene), "OnMatchCompleted")]
    internal static class BotBattleScene_OnMatchCompleted_Patch
    {
        [HarmonyPrefix]
        private static void Prefix()
        {
            BotBattleBridge.OnMatchCompleted();
        }
    }

    [HarmonyPatch(typeof(MatchSceneManager), "ExitMatchScene")]
    internal static class MatchSceneManager_ExitMatchScene_Patch
    {
        [HarmonyPrefix]
        private static bool Prefix()
        {
            if (BotBattleBridge.IsRunning)
            {
                Plugin._log?.LogInfo("[HarmonyPatch] MatchSceneManager.ExitMatchScene prefix called! Skipping ExitMatchScene since bot battle is running.");
                return false; // Skip original method
            }
            return true; // Run original method
        }
    }

    [HarmonyPatch]
    internal static class BotBattleScene_ConnectConfig_Patch
    {
        [HarmonyTargetMethod]
        private static MethodBase TargetMethod()
        {
            return typeof(BotBattleScene).GetMethod("ConnectConfig", BindingFlags.NonPublic | BindingFlags.Instance);
        }

        [HarmonyPrefix]
        private static void Prefix()
        {
            if (Wizards.Mtga.Pantry.CurrentEnvironment != null)
            {
                if (string.IsNullOrEmpty(Wizards.Mtga.Pantry.CurrentEnvironment.mdHost) || Wizards.Mtga.Pantry.CurrentEnvironment.mdPort <= 0)
                {
                    Plugin._log?.LogInfo($"[HarmonyPatch] BotBattleScene.ConnectConfig: mdHost is empty. Overwriting with fdHost: {Wizards.Mtga.Pantry.CurrentEnvironment.fdHost}:{Wizards.Mtga.Pantry.CurrentEnvironment.fdPort}");
                    Wizards.Mtga.Pantry.CurrentEnvironment.mdHost = Wizards.Mtga.Pantry.CurrentEnvironment.fdHost;
                    Wizards.Mtga.Pantry.CurrentEnvironment.mdPort = Wizards.Mtga.Pantry.CurrentEnvironment.fdPort;
                }
            }
        }
    }
}
