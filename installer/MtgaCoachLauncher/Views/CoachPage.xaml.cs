using System.Text;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
using MtgaCoachLauncher.Services;
using Windows.ApplicationModel.DataTransfer;

namespace MtgaCoachLauncher.Views;

public partial class CoachPage : Page
{
    private CoachProcess? _process;
    private MainPage? _mainPage;
    private readonly DispatcherQueue _dispatcher;
    private int _logCount;
    private readonly List<string> _allLogLines = [];
    private string _lastGameState = "";

    public CoachPage()
    {
        this.InitializeComponent();
        this.NavigationCacheMode = NavigationCacheMode.Disabled;
        _dispatcher = DispatcherQueue.GetForCurrentThread();
    }

    public void AttachMainPage(MainPage mainPage)
        => _mainPage = mainPage;

    public void AttachProcess(CoachProcess process)
    {
        if (ReferenceEquals(_process, process))
            return;

        DetachProcess();
        _process = process;
        _process.EventReceived += OnEvent;
        _process.Exited += OnProcessExited;
        _process.StderrLine += OnStderrLine;
        AppendLog("Coach process started.");
    }

    public void DetachProcess()
    {
        if (_process is not null)
        {
            _process.EventReceived -= OnEvent;
            _process.Exited -= OnProcessExited;
            _process.StderrLine -= OnStderrLine;
            _process = null;
        }
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        base.OnNavigatedFrom(e);
        DetachProcess();
    }

    // ── Event handling ──────────────────────────────────────────────

    private void OnEvent(JsonElement evt)
    {
        // Use High priority so events render even when window is unfocused.
        // Default/Normal priority gets throttled by WinUI when the window
        // doesn't have input focus, causing the entire UI to appear frozen.
        _dispatcher.TryEnqueue(DispatcherQueuePriority.High, () =>
        {
            try
            {
                HandleEvent(evt);
            }
            catch (Exception ex)
            {
                var type = evt.TryGetProperty("type", out var typeProp) ? typeProp.ToString() : "<unknown>";
                CrashLogger.LogException(
                    $"CoachPage.OnEvent type={type} payload={Truncate(evt.GetRawText(), 800)}",
                    ex);
            }
        });
    }

    private void OnStderrLine(string line)
    {
        _dispatcher.TryEnqueue(DispatcherQueuePriority.High, () =>
        {
            try
            {
                AppendLog($"[stderr] {line}");
            }
            catch (Exception ex)
            {
                CrashLogger.LogException($"CoachPage.OnStderrLine line={Truncate(line, 400)}", ex);
            }
        });
    }

    private void HandleEvent(JsonElement evt)
    {
        if (!evt.TryGetProperty("type", out var typeProp)) return;
        var type = typeProp.ValueKind == JsonValueKind.String ? typeProp.GetString() : null;

        switch (type)
        {
            case "log":
                AppendLog(GetStringOrDefault(evt, "message"), "dim");
                break;
            case "advice":
                var seatInfo = GetStringOrDefault(evt, "seat_info");
                var adviceText = GetStringOrDefault(evt, "text");
                AppendLog($"COACH ({seatInfo})", "header");
                AppendLog(adviceText, "advice");
                break;
            case "status":
                UpdateStatus(
                    GetStringOrDefault(evt, "key"),
                    GetStringOrDefault(evt, "value"));
                break;
            case "error":
                AppendLog($"ERROR: {GetStringOrDefault(evt, "message")}", "error");
                break;
            case "subtask":
                var status = GetStringOrDefault(evt, "status");
                if (!string.IsNullOrEmpty(status))
                    AppendLog($"  > {status}", "status");
                break;
            case "game_state":
                if (evt.TryGetProperty("data", out var data))
                    UpdateGameState(data);
                break;
            case "speak_audio":
                HandleSpeakAudio(evt);
                break;
            case "speak_stop":
                AudioPlayback.Stop();
                break;
        }
    }

    private void HandleSpeakAudio(JsonElement evt)
    {
        var path = GetStringOrDefault(evt, "path");
        var text = GetStringOrDefault(evt, "text");

        if (string.IsNullOrWhiteSpace(path))
        {
            CrashLogger.LogBreadcrumb($"CoachPage.HandleSpeakAudio missing path text={Truncate(text, 200)}");
            return;
        }

        AudioPlayback.PlayFile(path, Truncate(text, 200));
    }

    private void AppendLog(string text, string color = "default")
    {
        try
        {
            _allLogLines.Add(text);

            var brush = color switch
            {
                "advice" => new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 100, 220, 100)),  // green
                "header" => new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 180, 140, 255)),  // purple
                "error" => new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 255, 100, 100)),   // red
                "status" => new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 100, 200, 220)),  // cyan
                "dim" => new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.ColorHelper.FromArgb(255, 130, 130, 130)),     // gray
                _ => null,
            };

            var tb = new TextBlock
            {
                Text = text,
                TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap,
                FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Consolas"),
                FontSize = color == "advice" ? 14 : 13,
                FontWeight = color == "advice" ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Normal,
                Padding = new Thickness(12, color == "advice" ? 6 : 3, 12, color == "advice" ? 6 : 3),
                IsTextSelectionEnabled = true,
            };
            if (brush is not null) tb.Foreground = brush;
            LogPanel.Children.Add(tb);
            _logCount++;

            while (_logCount > 500 && LogPanel.Children.Count > 0)
            {
                LogPanel.Children.RemoveAt(0);
                _logCount--;
            }

            _dispatcher.TryEnqueue(DispatcherQueuePriority.Normal, () =>
            {
                try
                {
                    if (this.XamlRoot is null)
                        return;

                    AdviceScroller.ChangeView(null, AdviceScroller.ScrollableHeight, null);
                }
                catch (Exception ex)
                {
                    CrashLogger.LogException("CoachPage.AppendLog.ChangeView", ex);
                }
            });
        }
        catch (Exception ex)
        {
            CrashLogger.LogException($"CoachPage.AppendLog color={color} text={Truncate(text, 400)}", ex);
        }
    }

    private void UpdateStatus(string key, string value)
    {
        switch (key.ToUpperInvariant())
        {
            case "SEAT_INFO": StatusSeat.Text = value; break;
            case "BACKEND":
                StatusBackend.Text = value;
                BtnMode.Content = value.Contains("online") ? "Online" : value.Contains("local") ? "Local" : value;
                break;
            case "MODEL":
                StatusModel.Text = value;
                BtnModel.Content = string.IsNullOrEmpty(value) ? "Model" : value;
                break;
            case "BRIDGE": case "GRE": StatusBridge.Text = value; break;
            case "VOICE": case "VOICE_ID":
                BtnVoice.Content = string.IsNullOrEmpty(value) ? "Voice [F6]" : $"{value} [F6]";
                break;
            case "SPEED":
                BtnSpeed.Content = string.IsNullOrEmpty(value) ? "1.0x [F8]" : $"{value} [F8]";
                break;
            case "AUTOPILOT":
                BtnAutopilot.Content = $"{value} [F12]";
                break;
            case "MUTE":
                BtnMute.Content = value.Contains("Muted") ? "Unmute [F5]" : "Mute [F5]";
                break;
            case "STYLE":
                BtnStyle.Content = $"{value} [F2]";
                break;
            case "AFK":
                BtnAfk.Content = $"AFK:{value} [F9]";
                break;
            case "LAND_ONLY":
                BtnLandOnly.Content = $"Land:{value} [F10]";
                break;
        }
    }

    private void UpdateGameState(JsonElement data)
    {
        try
        {
            var sb = new StringBuilder();

            // --- Header: Turn / Phase / Life ---
            int localSeat = 0;
            if (data.TryGetProperty("turn", out var turn))
            {
                var turnNum = GetInt32OrDefault(turn, "turn_number");
                var phase = GetStringOrDefault(turn, "phase");
                var step = GetStringOrDefault(turn, "step");
                var activePlayer = GetInt32OrDefault(turn, "active_player");
                sb.Append($"Turn {turnNum}  {phase}");
                if (!string.IsNullOrEmpty(step)) sb.Append($" / {step}");
                localSeat = GetInt32OrDefault(data, "local_seat_id");
                sb.Append(activePlayer == localSeat ? "  (your turn)" : "  (opp turn)");
            }

            if (data.TryGetProperty("players", out var players) && players.ValueKind == JsonValueKind.Array)
            {
                sb.Append("   |   ");
                foreach (var p in players.EnumerateArray())
                {
                    var life = GetInt32OrDefault(p, "life_total");
                    var isLocal = GetBooleanOrDefault(p, "is_local");
                    var label = isLocal ? "YOU" : "OPP";
                    sb.Append($"{label}: {life}  ");
                }
            }

            sb.AppendLine();

            if (data.TryGetProperty("pending_decision", out var pd) &&
                pd.ValueKind == JsonValueKind.String &&
                !string.IsNullOrEmpty(pd.GetString()))
            {
                sb.AppendLine($">>> {pd.GetString()}");
            }

            if (data.TryGetProperty("zones", out var zones) && zones.ValueKind == JsonValueKind.Object)
            {
            // --- Hand (with mana costs) ---
            if (zones.TryGetProperty("my_hand", out var hand) && hand.ValueKind == JsonValueKind.Array)
            {
                var cards = new List<string>();
                foreach (var c in hand.EnumerateArray())
                {
                    var name = GetStringOrDefault(c, "name", "?");
                    var cost = GetStringOrDefault(c, "mana_cost");
                    cards.Add(!string.IsNullOrEmpty(cost) ? $"{name} ({cost})" : name ?? "?");
                }
                if (cards.Count > 0)
                    sb.AppendLine($"Hand ({cards.Count}): {string.Join(", ", cards)}");
            }

            // --- Battlefield (detailed per-card) ---
            if (zones.TryGetProperty("battlefield", out var bf) && bf.ValueKind == JsonValueKind.Array)
            {
                var yours = new List<string>();
                var opps = new List<string>();
                foreach (var card in bf.EnumerateArray())
                {
                    var name = GetStringOrDefault(card, "name", "?");
                    var owner = GetInt32OrDefault(card, "owner_seat_id");
                    var tapped = GetBooleanOrDefault(card, "is_tapped");
                    var typeLine = GetStringOrDefault(card, "type_line");
                    var parts = new List<string> { name };

                    if (typeLine.Contains("Creature", StringComparison.OrdinalIgnoreCase))
                    {
                        var pow = GetInt32OrDefault(card, "power");
                        var tou = GetInt32OrDefault(card, "toughness");
                        parts.Add($"{pow}/{tou}");
                    }
                    if (typeLine.Contains("Planeswalker", StringComparison.OrdinalIgnoreCase) &&
                        card.TryGetProperty("counters", out var pctrs) && pctrs.ValueKind == JsonValueKind.Object &&
                        pctrs.TryGetProperty("Loyalty", out var loy))
                        parts.Add($"Loy:{GetInt32OrDefault(loy)}");

                    if (tapped) parts.Add("T");

                    if (card.TryGetProperty("counters", out var counters) && counters.ValueKind == JsonValueKind.Object)
                        foreach (var ctr in counters.EnumerateObject())
                            if (ctr.Name != "Loyalty") parts.Add($"+{GetInt32OrDefault(ctr.Value)} {ctr.Name}");

                    if (GetBooleanOrDefault(card, "is_attacking")) parts.Add("ATK");
                    if (GetBooleanOrDefault(card, "is_blocking")) parts.Add("BLK");

                    (owner == localSeat ? yours : opps).Add(string.Join(" ", parts));
                }
                sb.AppendLine($"Your Board ({yours.Count}): {string.Join(", ", yours)}");
                if (opps.Count > 0)
                    sb.AppendLine($"Opp Board ({opps.Count}): {string.Join(", ", opps)}");
            }

            // --- Stack ---
            if (zones.TryGetProperty("stack", out var stack) && stack.ValueKind == JsonValueKind.Array && stack.GetArrayLength() > 0)
            {
                var items = new List<string>();
                foreach (var s in stack.EnumerateArray())
                    items.Add(GetStringOrDefault(s, "name", "?"));
                sb.AppendLine($"Stack: {string.Join(" -> ", items)}");
            }

            // --- Graveyard ---
            if (zones.TryGetProperty("graveyard", out var gy) && gy.ValueKind == JsonValueKind.Array && gy.GetArrayLength() > 0)
            {
                var yours = new List<string>();
                var opps = new List<string>();
                foreach (var card in gy.EnumerateArray())
                {
                    var name = GetStringOrDefault(card, "name", "?");
                    var owner = GetInt32OrDefault(card, "owner_seat_id");
                    (owner == localSeat ? yours : opps).Add(name);
                }
                if (yours.Count > 0) sb.AppendLine($"Your GY ({yours.Count}): {string.Join(", ", yours)}");
                if (opps.Count > 0) sb.AppendLine($"Opp GY ({opps.Count}): {string.Join(", ", opps)}");
            }

            // --- Exile ---
            if (zones.TryGetProperty("exile", out var ex) && ex.ValueKind == JsonValueKind.Array && ex.GetArrayLength() > 0)
            {
                var items = new List<string>();
                foreach (var card in ex.EnumerateArray())
                    items.Add(GetStringOrDefault(card, "name", "?"));
                sb.AppendLine($"Exile ({items.Count}): {string.Join(", ", items)}");
            }

            // --- Command zone ---
            if (zones.TryGetProperty("command", out var cmd) && cmd.ValueKind == JsonValueKind.Array && cmd.GetArrayLength() > 0)
            {
                var items = new List<string>();
                foreach (var card in cmd.EnumerateArray())
                    items.Add(GetStringOrDefault(card, "name", "?"));
                sb.AppendLine($"Command: {string.Join(", ", items)}");
            }

            // --- Library count ---
            if (zones.TryGetProperty("library_count", out var lib))
                sb.AppendLine($"Library: {GetInt32OrDefault(lib)} cards");
        }

        // --- Legal actions (non-trivial only) ---
        if (data.TryGetProperty("legal_actions", out var la) && la.ValueKind == JsonValueKind.Array && la.GetArrayLength() > 0)
        {
            var actions = new List<string>();
            foreach (var a in la.EnumerateArray())
            {
                var s = a.GetString();
                if (s != null && s != "Pass" && !s.StartsWith("Action: Activate_Mana") && !s.StartsWith("Action: FloatMana"))
                    actions.Add(s);
            }
            if (actions.Count > 0)
                sb.AppendLine($"Legal ({actions.Count}): {string.Join(", ", actions)}");
        }

            var text = sb.ToString().TrimEnd();
            GameStateText.Text = text;
            _lastGameState = text;
        }
        catch (Exception ex)
        {
            CrashLogger.LogException(
                $"CoachPage.UpdateGameState payload={Truncate(data.GetRawText(), 1200)}",
                ex);
        }
    }

    private void OnProcessExited(int exitCode)
    {
        _dispatcher.TryEnqueue(() =>
        {
            try
            {
                AppendLog("Restarting coach...");
                DetachProcess();
                // Auto-restart the coach process
                _mainPage?.RestartCoach(false, false, false);
            }
            catch (Exception ex)
            {
                CrashLogger.LogException($"CoachPage.OnProcessExited exitCode={exitCode}", ex);
            }
        });
    }

    private static string Truncate(string? text, int maxLength)
    {
        if (string.IsNullOrEmpty(text) || text.Length <= maxLength)
            return text ?? "";

        return text[..maxLength] + "...";
    }

    private static string GetStringOrDefault(JsonElement parent, string propertyName, string fallback = "")
    {
        if (!parent.TryGetProperty(propertyName, out var value) || value.ValueKind != JsonValueKind.String)
            return fallback;

        return value.GetString() ?? fallback;
    }

    private static int GetInt32OrDefault(JsonElement parent, string propertyName, int fallback = 0)
    {
        if (!parent.TryGetProperty(propertyName, out var value))
            return fallback;

        return GetInt32OrDefault(value, fallback);
    }

    private static int GetInt32OrDefault(JsonElement value, int fallback = 0)
    {
        return value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number)
            ? number
            : fallback;
    }

    private static bool GetBooleanOrDefault(JsonElement parent, string propertyName, bool fallback = false)
    {
        if (!parent.TryGetProperty(propertyName, out var value))
            return fallback;

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => fallback,
        };
    }

    private void SendProcessCommand(string source, string command)
    {
        CrashLogger.LogBreadcrumb($"CoachPage command source={source} cmd={command}");
        _process?.SendCommand(command);
    }

    // ── Button handlers ─────────────────────────────────────────────

    private void Mode_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Mode", "cycle_mode");
    private void Model_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Model", "cycle_model");
    private void Style_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Style", "toggle_style");
    private void Voice_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Voice", "cycle_voice");
    private void Speed_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Speed", "cycle_speed");
    private void Mute_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Mute", "toggle_mute");
    private void Autopilot_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Autopilot", "toggle_autopilot");
    private void AutopilotCancel_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("AutopilotCancel", "autopilot_cancel");
    private void AutopilotAbort_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("AutopilotAbort", "autopilot_abort");
    private void Afk_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Afk", "toggle_afk");
    private void LandOnly_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("LandOnly", "toggle_land_only");
    private void Screen_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Screen", "analyze_screen");
    private void WinPlan_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("WinPlan", "read_win_plan");
    private void Restart_Click(object sender, RoutedEventArgs e)
        => SendProcessCommand("Restart", "restart");

    private async void CopyDebug_Click(object sender, RoutedEventArgs e)
    {
        var sb = new StringBuilder();
        sb.AppendLine("=== mtgacoach Debug Logs ===");
        sb.AppendLine($"Timestamp: {DateTime.Now:yyyy-MM-dd HH:mm:ss}");
        sb.AppendLine();

        // Game state
        sb.AppendLine("--- Game State ---");
        sb.AppendLine(_lastGameState);
        sb.AppendLine();

        // Coach log (last 100 lines)
        sb.AppendLine("--- Coach Log (last 100) ---");
        var start = Math.Max(0, _allLogLines.Count - 100);
        for (int i = start; i < _allLogLines.Count; i++)
            sb.AppendLine(_allLogLines[i]);
        sb.AppendLine();

        // Player.log tail
        sb.AppendLine("--- Player.log tail ---");
        var state = RuntimeDetector.DetectRuntimeState();
        sb.AppendLine(RuntimeDetector.TailText(state.PlayerLog, 4000));
        sb.AppendLine();

        // BepInEx log tail
        sb.AppendLine("--- BepInEx LogOutput.log tail ---");
        sb.AppendLine(RuntimeDetector.TailText(state.BepinexLog, 4000));
        sb.AppendLine();

        // standalone.log tail
        var standaloneLog = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".arenamcp", "standalone.log");
        sb.AppendLine("--- standalone.log tail ---");
        sb.AppendLine(RuntimeDetector.TailText(standaloneLog, 4000));
        sb.AppendLine();

        // Crash log
        var crashLog = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "mtgacoach", "crash.log");
        if (File.Exists(crashLog))
        {
            sb.AppendLine("--- crash.log ---");
            sb.AppendLine(RuntimeDetector.TailText(crashLog, 2000));
        }

        var debugText = sb.ToString();

        // Save to file
        var debugFile = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "mtgacoach", "debug_dump.txt");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(debugFile)!);
            File.WriteAllText(debugFile, debugText);
        }
        catch { }

        // Copy to clipboard
        var dp = new DataPackage();
        dp.SetText(debugText);
        Clipboard.SetContent(dp);

        // Also trigger Python-side bug report (includes replay, autopilot, bridge state)
        _process?.SendCommand("debug_report");

        AppendLog($"Debug logs copied to clipboard and saved to {debugFile}");
    }

    private void ChatSend_Click(object sender, RoutedEventArgs e)
        => SendChat();

    private void ChatInput_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Enter)
        {
            SendChat();
            e.Handled = true;
        }
    }

    private void SendChat()
    {
        var text = ChatInput.Text?.Trim();
        if (string.IsNullOrEmpty(text)) return;
        ChatInput.Text = "";
        AppendLog($"> {text}");
        _process?.SendCommand("chat", text);
    }
}
