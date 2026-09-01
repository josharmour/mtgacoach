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
        // get_card_positions â€” return on-screen rectangles for every visible
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
                // DuelScene_CDC extends BASE_CDC : MonoBehaviour â€” every
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
}
