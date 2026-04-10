using System.Text;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Navigation;
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
        this.NavigationCacheMode = NavigationCacheMode.Required;
        _dispatcher = DispatcherQueue.GetForCurrentThread();
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _mainPage = e.Parameter as MainPage;
    }

    public void AttachProcess(CoachProcess process)
    {
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

    // ── F-key handler ───────────────────────────────────────────────

    private void Grid_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        // Don't intercept keys while typing in the chat box
        if (ChatInput.FocusState != Microsoft.UI.Xaml.FocusState.Unfocused)
            return;

        switch (e.Key)
        {
            case Windows.System.VirtualKey.F1:
                _process?.SendCommand("autopilot_cancel");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F2:
                _process?.SendCommand("toggle_style");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F3:
                _process?.SendCommand("analyze_screen");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F4:
                _process?.SendCommand("autopilot_abort");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F5:
                _process?.SendCommand("toggle_mute");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F6:
                _process?.SendCommand("cycle_voice");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F7:
                CopyDebug_Click(sender, new RoutedEventArgs());
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F8:
                _process?.SendCommand("cycle_speed");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F9:
                _process?.SendCommand("toggle_afk");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F10:
                _process?.SendCommand("toggle_land_only");
                e.Handled = true;
                break;
            case Windows.System.VirtualKey.F12:
                _process?.SendCommand("toggle_autopilot");
                e.Handled = true;
                break;
        }
    }

    // ── Event handling ──────────────────────────────────────────────

    private void OnEvent(JsonElement evt)
    {
        _dispatcher.TryEnqueue(() => HandleEvent(evt));
    }

    private void OnStderrLine(string line)
    {
        _dispatcher.TryEnqueue(() => AppendLog($"[stderr] {line}"));
    }

    private void HandleEvent(JsonElement evt)
    {
        if (!evt.TryGetProperty("type", out var typeProp)) return;
        var type = typeProp.GetString();

        switch (type)
        {
            case "log":
                AppendLog(evt.GetProperty("message").GetString() ?? "", "dim");
                break;
            case "advice":
                var seatInfo = evt.GetProperty("seat_info").GetString() ?? "";
                var adviceText = evt.GetProperty("text").GetString() ?? "";
                AppendLog($"COACH ({seatInfo})", "header");
                AppendLog(adviceText, "advice");
                break;
            case "status":
                UpdateStatus(
                    evt.GetProperty("key").GetString() ?? "",
                    evt.GetProperty("value").GetString() ?? "");
                break;
            case "error":
                AppendLog($"ERROR: {evt.GetProperty("message").GetString()}", "error");
                break;
            case "subtask":
                var status = evt.GetProperty("status").GetString() ?? "";
                if (!string.IsNullOrEmpty(status))
                    AppendLog($"  > {status}", "status");
                break;
            case "game_state":
                if (evt.TryGetProperty("data", out var data))
                    UpdateGameState(data);
                break;
        }
    }

    private void AppendLog(string text, string color = "default")
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

        _dispatcher.TryEnqueue(DispatcherQueuePriority.Low, () =>
        {
            AdviceScroller.ChangeView(null, AdviceScroller.ScrollableHeight, null);
        });
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
        var sb = new StringBuilder();

        // --- Header: Turn / Phase / Life ---
        int localSeat = 0;
        if (data.TryGetProperty("turn", out var turn))
        {
            var turnNum = turn.TryGetProperty("turn_number", out var tn) ? tn.GetInt32() : 0;
            var phase = turn.TryGetProperty("phase", out var ph) ? ph.GetString() : "";
            var step = turn.TryGetProperty("step", out var st) ? st.GetString() : "";
            var activePlayer = turn.TryGetProperty("active_player", out var ap) ? ap.GetInt32() : 0;
            sb.Append($"Turn {turnNum}  {phase}");
            if (!string.IsNullOrEmpty(step)) sb.Append($" / {step}");
            if (data.TryGetProperty("local_seat_id", out var ls))
                localSeat = ls.GetInt32();
            sb.Append(activePlayer == localSeat ? "  (your turn)" : "  (opp turn)");
        }

        if (data.TryGetProperty("players", out var players) && players.ValueKind == JsonValueKind.Array)
        {
            sb.Append("   |   ");
            foreach (var p in players.EnumerateArray())
            {
                var life = p.TryGetProperty("life_total", out var lt) ? lt.GetInt32() : 0;
                var isLocal = p.TryGetProperty("is_local", out var il) && il.GetBoolean();
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

        if (data.TryGetProperty("zones", out var zones))
        {
            // --- Hand (with mana costs) ---
            if (zones.TryGetProperty("my_hand", out var hand) && hand.ValueKind == JsonValueKind.Array)
            {
                var cards = new List<string>();
                foreach (var c in hand.EnumerateArray())
                {
                    var name = c.TryGetProperty("name", out var n) ? n.GetString() : "?";
                    var cost = c.TryGetProperty("mana_cost", out var mc) ? mc.GetString() : "";
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
                    var name = card.TryGetProperty("name", out var n) ? n.GetString() ?? "?" : "?";
                    var owner = card.TryGetProperty("owner_seat_id", out var o) ? o.GetInt32() : 0;
                    var tapped = card.TryGetProperty("is_tapped", out var t) && t.GetBoolean();
                    var typeLine = card.TryGetProperty("type_line", out var tl) ? tl.GetString() ?? "" : "";
                    var parts = new List<string> { name };

                    if (typeLine.Contains("Creature", StringComparison.OrdinalIgnoreCase))
                    {
                        var pow = card.TryGetProperty("power", out var pw) ? pw.GetInt32() : 0;
                        var tou = card.TryGetProperty("toughness", out var to2) ? to2.GetInt32() : 0;
                        parts.Add($"{pow}/{tou}");
                    }
                    if (typeLine.Contains("Planeswalker", StringComparison.OrdinalIgnoreCase) &&
                        card.TryGetProperty("counters", out var pctrs) && pctrs.ValueKind == JsonValueKind.Object &&
                        pctrs.TryGetProperty("Loyalty", out var loy))
                        parts.Add($"Loy:{loy.GetInt32()}");

                    if (tapped) parts.Add("T");

                    if (card.TryGetProperty("counters", out var counters) && counters.ValueKind == JsonValueKind.Object)
                        foreach (var ctr in counters.EnumerateObject())
                            if (ctr.Name != "Loyalty") parts.Add($"+{ctr.Value.GetInt32()} {ctr.Name}");

                    if (card.TryGetProperty("is_attacking", out var atk) && atk.GetBoolean()) parts.Add("ATK");
                    if (card.TryGetProperty("is_blocking", out var blk) && blk.GetBoolean()) parts.Add("BLK");

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
                    items.Add(s.TryGetProperty("name", out var n) ? n.GetString() ?? "?" : "?");
                sb.AppendLine($"Stack: {string.Join(" -> ", items)}");
            }

            // --- Graveyard ---
            if (zones.TryGetProperty("graveyard", out var gy) && gy.ValueKind == JsonValueKind.Array && gy.GetArrayLength() > 0)
            {
                var yours = new List<string>();
                var opps = new List<string>();
                foreach (var card in gy.EnumerateArray())
                {
                    var name = card.TryGetProperty("name", out var n) ? n.GetString() ?? "?" : "?";
                    var owner = card.TryGetProperty("owner_seat_id", out var o) ? o.GetInt32() : 0;
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
                    items.Add(card.TryGetProperty("name", out var n) ? n.GetString() ?? "?" : "?");
                sb.AppendLine($"Exile ({items.Count}): {string.Join(", ", items)}");
            }

            // --- Command zone ---
            if (zones.TryGetProperty("command", out var cmd) && cmd.ValueKind == JsonValueKind.Array && cmd.GetArrayLength() > 0)
            {
                var items = new List<string>();
                foreach (var card in cmd.EnumerateArray())
                    items.Add(card.TryGetProperty("name", out var n) ? n.GetString() ?? "?" : "?");
                sb.AppendLine($"Command: {string.Join(", ", items)}");
            }

            // --- Library count ---
            if (zones.TryGetProperty("library_count", out var lib))
                sb.AppendLine($"Library: {lib} cards");
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

    private void OnProcessExited(int exitCode)
    {
        _dispatcher.TryEnqueue(() =>
        {
            AppendLog("Restarting coach...");
            DetachProcess();
            // Auto-restart the coach process
            _mainPage?.RestartCoach(false, false, false);
        });
    }

    // ── Button handlers ─────────────────────────────────────────────

    private void Mode_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_mode");
    private void Model_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_model");
    private void Style_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_style");
    private void Voice_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_voice");
    private void Speed_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_speed");
    private void Mute_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_mute");
    private void Autopilot_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_autopilot");
    private void AutopilotCancel_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("autopilot_cancel");
    private void AutopilotAbort_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("autopilot_abort");
    private void Afk_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_afk");
    private void LandOnly_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_land_only");
    private void Screen_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("analyze_screen");
    private void WinPlan_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("read_win_plan");
    private void Restart_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("restart");

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
