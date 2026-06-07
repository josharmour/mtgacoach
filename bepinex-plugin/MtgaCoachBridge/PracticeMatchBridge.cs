using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using BepInEx.Logging;
using Newtonsoft.Json.Linq;
using UnityEngine;
using Wizards.Mtga;
using Wizards.Mtga.Decks;

namespace MtgaCoachBridge
{
    // Starts a real "Play vs AI" (AIBotMatch) practice match. Unlike the dead
    // start_bot_battle path, this goes through real matchmaking and runs on a
    // real game server by invoking HomePageContentController.JoinMatchMaking
    // with the "AIBotMatch" event name.
    internal static class PracticeMatchBridge
    {
        public static JObject Start(JObject json, ManualLogSource log)
        {
            try
            {
                // Readiness: card database must be loaded (mirror BotBattleBridge).
                var papa = UnityEngine.Object.FindObjectOfType<PAPA>();
                if (papa == null)
                {
                    return Fail(log, "PAPA not found — is MTGA past the title screen?");
                }
                if (papa.CardDatabase == null)
                {
                    return Fail(log, "PAPA.CardDatabase is null — db not loaded yet?");
                }

                // JoinMatchMaking lives on the Home-screen MonoBehaviour.
                var home = UnityEngine.Object.FindObjectOfType<HomePageContentController>();
                if (home == null)
                {
                    return Fail(log, "not at Home screen — log in and return to Home");
                }

                var deckProvider = Pantry.Get<DeckDataProvider>();
                if (deckProvider == null)
                {
                    return Fail(log, "DeckDataProvider not available from Pantry");
                }

                List<Client_Deck> allDecks = deckProvider.GetCachedDecks() ?? new List<Client_Deck>();
                if (allDecks.Count == 0)
                {
                    return Fail(log, "no decks found — create a constructed deck first");
                }

                string deckName = json.Value<string>("deck_name");
                string deckIdStr = json.Value<string>("deck_id");

                Client_Deck chosen = null;

                // Explicit deck_id wins if supplied.
                if (!string.IsNullOrWhiteSpace(deckIdStr))
                {
                    if (!Guid.TryParse(deckIdStr, out var parsedId))
                    {
                        return Fail(log, $"deck_id is not a valid GUID: '{deckIdStr}'");
                    }
                    chosen = deckProvider.GetDeckForId(parsedId);
                    if (chosen == null)
                    {
                        return Fail(log, $"no deck with id '{deckIdStr}'. Available: {DeckNames(allDecks)}");
                    }
                }
                else if (!string.IsNullOrWhiteSpace(deckName))
                {
                    chosen = allDecks.FirstOrDefault(d =>
                        d?.Summary != null &&
                        string.Equals(d.Summary.Name, deckName, StringComparison.OrdinalIgnoreCase));
                    if (chosen == null)
                    {
                        return Fail(log, $"no deck named '{deckName}'. Available: {DeckNames(allDecks)}");
                    }
                }
                else
                {
                    chosen = PickDefaultDeck(allDecks);
                    if (chosen == null)
                    {
                        return Fail(log, $"no valid constructed deck to default to. Available: {DeckNames(allDecks)}");
                    }
                }

                if (chosen == null)
                {
                    return Fail(log, $"deck selection failed. Available: {DeckNames(allDecks)}");
                }

                Guid deckId = chosen.Id;
                string resolvedName = chosen.Summary != null ? chosen.Summary.Name : string.Empty;

                log?.LogInfo($"[PracticeMatchBridge] Starting AIBotMatch with deck '{resolvedName}' ({deckId})");

                // JoinMatchMaking(string internalEventName, Guid deckId) is private instance.
                var method = typeof(HomePageContentController).GetMethod(
                    "JoinMatchMaking",
                    BindingFlags.NonPublic | BindingFlags.Instance);
                if (method == null)
                {
                    return Fail(log, "HomePageContentController.JoinMatchMaking not found via reflection");
                }

                method.Invoke(home, new object[] { "AIBotMatch", deckId });

                return new JObject
                {
                    ["ok"] = true,
                    ["deck_id"] = deckId.ToString(),
                    ["deck_name"] = resolvedName,
                    ["event"] = "AIBotMatch",
                };
            }
            catch (Exception ex)
            {
                log?.LogError($"[PracticeMatchBridge] Start failed: {ex}");
                return Fail(log, $"unhandled: {ex.InnerException?.Message ?? ex.Message}");
            }
        }

        // Returns to the Home screen from the post-match result screen by
        // invoking the same call the "Leave Match" button fires
        // (MatchEndScene.LeaveMatch -> ExitMatchCompleted). Lets the harness
        // loop matches with no clicks.
        public static JObject ReturnToHome(ManualLogSource log)
        {
            try
            {
                var endScene = UnityEngine.Object.FindObjectOfType<MatchEndScene>();
                if (endScene == null)
                {
                    return Fail(log, "no MatchEndScene active — not on the match-result screen");
                }
                endScene.LeaveMatch();
                log?.LogInfo("[PracticeMatchBridge] ReturnToHome: LeaveMatch() invoked");
                return new JObject { ["ok"] = true };
            }
            catch (Exception ex)
            {
                return Fail(log, $"ReturnToHome failed: {ex.InnerException?.Message ?? ex.Message}");
            }
        }

        // Default: prefer a complete constructed deck (>= 60 main cards), most
        // recently played first. If contents aren't loaded for any cached deck
        // (main count unknown / 0), fall back to the most recently played deck.
        private static Client_Deck PickDefaultDeck(List<Client_Deck> decks)
        {
            var constructed = decks
                .Where(d => d != null && MainCount(d) >= 60)
                .OrderByDescending(LastPlayed)
                .ToList();
            if (constructed.Count > 0)
            {
                return constructed[0];
            }

            // Contents likely not loaded; can't gauge size — pick most recent.
            return decks
                .Where(d => d != null)
                .OrderByDescending(LastPlayed)
                .FirstOrDefault();
        }

        private static int MainCount(Client_Deck deck)
        {
            try
            {
                if (deck?.Contents?.Piles != null &&
                    deck.Contents.Piles.TryGetValue(EDeckPile.Main, out var pile) &&
                    pile != null)
                {
                    long total = 0;
                    foreach (var card in pile)
                    {
                        total += card.Quantity;
                    }
                    return total > int.MaxValue ? int.MaxValue : (int)total;
                }
            }
            catch
            {
                // ignore — treat as unknown size
            }
            return 0;
        }

        private static DateTime LastPlayed(Client_Deck deck)
        {
            try
            {
                return deck?.Summary != null ? deck.Summary.LastPlayed : DateTime.MinValue;
            }
            catch
            {
                return DateTime.MinValue;
            }
        }

        private static string DeckNames(List<Client_Deck> decks)
        {
            var names = decks
                .Where(d => d?.Summary != null && !string.IsNullOrEmpty(d.Summary.Name))
                .Select(d => d.Summary.Name)
                .ToList();
            return names.Count > 0 ? string.Join(", ", names) : "(none)";
        }

        private static JObject Fail(ManualLogSource log, string msg)
        {
            log?.LogWarning($"[PracticeMatchBridge] {msg}");
            return new JObject { ["ok"] = false, ["error"] = msg };
        }
    }
}
