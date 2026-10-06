using System;
using BepInEx.Logging;
using Newtonsoft.Json.Linq;

namespace MtgaCoachBridge
{
    internal static class BotBattleBridge
    {
        public static bool IsRunning => false;

        public static JObject Start(JObject json, ManualLogSource log)
        {
            log?.LogWarning("[BotBattleBridge] BotBattleScene was removed by MTGA. Use PracticeMatchBridge instead.");
            return new JObject
            {
                ["ok"] = false,
                ["error"] = "BotBattleScene was removed in recent MTGA versions. Use start_practice_match instead."
            };
        }

        public static JObject GetStatus()
        {
            return new JObject
            {
                ["running"] = false,
                ["error"] = "BotBattleScene deprecated"
            };
        }

        public static void OnMatchCompleted() { }
    }
}
