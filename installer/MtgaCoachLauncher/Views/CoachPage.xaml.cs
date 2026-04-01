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
                AppendLog(evt.GetProperty("message").GetString() ?? "");
                break;
            case "advice":
                var seatInfo = evt.GetProperty("seat_info").GetString() ?? "";
                var adviceText = evt.GetProperty("text").GetString() ?? "";
                AppendLog($"--- COACH ({seatInfo}) ---");
                AppendLog(adviceText);
                AppendLog("");
                break;
            case "status":
                UpdateStatus(
                    evt.GetProperty("key").GetString() ?? "",
                    evt.GetProperty("value").GetString() ?? "");
                break;
            case "error":
                AppendLog($"ERROR: {evt.GetProperty("message").GetString()}");
                break;
            case "subtask":
                var status = evt.GetProperty("status").GetString() ?? "";
                if (!string.IsNullOrEmpty(status))
                    AppendLog($"  > {status}");
                break;
            case "game_state":
                if (evt.TryGetProperty("data", out var data))
                    UpdateGameState(data);
                break;
        }
    }

    private void AppendLog(string text)
    {
        _allLogLines.Add(text);

        var tb = new TextBlock
        {
            Text = text,
            TextWrapping = Microsoft.UI.Xaml.TextWrapping.Wrap,
            FontFamily = new Microsoft.UI.Xaml.Media.FontFamily("Consolas"),
            FontSize = 13,
            Padding = new Thickness(12, 3, 12, 3),
            IsTextSelectionEnabled = true,
        };
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
            case "BACKEND": StatusBackend.Text = value; break;
            case "MODEL": StatusModel.Text = value; break;
            case "BRIDGE": case "GRE": StatusBridge.Text = value; break;
        }
    }

    private void UpdateGameState(JsonElement data)
    {
        var sb = new StringBuilder();

        if (data.TryGetProperty("turn", out var turn))
        {
            var turnNum = turn.TryGetProperty("turn_number", out var tn) ? tn.GetInt32() : 0;
            var phase = turn.TryGetProperty("phase", out var ph) ? ph.GetString() : "";
            var step = turn.TryGetProperty("step", out var st) ? st.GetString() : "";
            sb.Append($"Turn {turnNum}  {phase}");
            if (!string.IsNullOrEmpty(step)) sb.Append($" / {step}");
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
            if (zones.TryGetProperty("my_hand", out var hand) && hand.ValueKind == JsonValueKind.Array)
            {
                var cards = new List<string>();
                foreach (var c in hand.EnumerateArray())
                {
                    var name = c.TryGetProperty("name", out var n) ? n.GetString() : "?";
                    cards.Add(name ?? "?");
                }
                if (cards.Count > 0)
                    sb.AppendLine($"Hand ({cards.Count}): {string.Join(", ", cards)}");
            }

            if (zones.TryGetProperty("battlefield", out var bf) && bf.ValueKind == JsonValueKind.Array)
                sb.Append($"Battlefield: {bf.GetArrayLength()} permanents");
        }

        var text = sb.ToString().TrimEnd();
        GameStateText.Text = text;
        _lastGameState = text;
    }

    private void OnProcessExited(int exitCode)
    {
        _dispatcher.TryEnqueue(() =>
        {
            AppendLog($"--- Coach process exited (code {exitCode}) ---");
            DetachProcess();
        });
    }

    // ── Button handlers ─────────────────────────────────────────────

    private void Mode_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_mode");
    private void Model_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("cycle_model");
    private void Mute_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_mute");
    private void Autopilot_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("toggle_autopilot");
    private void Screen_Click(object sender, RoutedEventArgs e)
        => _process?.SendCommand("analyze_screen");
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
