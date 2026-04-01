using System.Text;
using Microsoft.UI;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using MtgaCoachLauncher.Models;
using Windows.Storage.Pickers;

namespace MtgaCoachLauncher.Views;

public partial class RepairPage : Page
{
    private const string GitHubReleases = "https://github.com/josharmour/mtgacoach/releases";
    private MainPage? _mainPage;
    private RuntimeState? _state;

    private readonly Dictionary<string, TextBlock> _statusLabels = [];
    private static readonly string[] StatusKeys =
    [
        "Runtime Root", "Python Runtime", "MTGA Install", "MTGA Process",
        "BepInEx", "Bridge Plugin", "BepInEx Bundle", "Player.log", "Bridge Readiness",
    ];

    public RepairPage()
    {
        this.InitializeComponent();
        BuildStatusRows();
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _mainPage = e.Parameter as MainPage;
        if (_mainPage?.State is not null)
            UpdateState(_mainPage.State);
    }

    private void BuildStatusRows()
    {
        for (int i = 0; i < StatusKeys.Length; i++)
        {
            StatusGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var label = new TextBlock
            {
                Text = StatusKeys[i] + ":",
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetRow(label, i);
            Grid.SetColumn(label, 0);
            StatusGrid.Children.Add(label);

            var value = new TextBlock
            {
                Text = "...",
                TextWrapping = TextWrapping.Wrap,
                VerticalAlignment = VerticalAlignment.Center,
                IsTextSelectionEnabled = true,
            };
            Grid.SetRow(value, i);
            Grid.SetColumn(value, 1);
            StatusGrid.Children.Add(value);
            _statusLabels[StatusKeys[i]] = value;
        }
    }

    public void UpdateState(RuntimeState state)
    {
        _state = state;
        if (string.IsNullOrEmpty(MtgaPathBox.Text) && state.MtgaDir is not null)
            MtgaPathBox.Text = state.MtgaDir;

        SetStatus("Runtime Root",
            state.RuntimeVenvExists ? $"{state.RuntimeRoot} (venv ready)"
            : state.PythonExe is not null ? $"{state.RuntimeRoot} (using {state.PythonSource})"
            : $"{state.RuntimeRoot} (setup required)",
            state.RuntimeVenvExists ? "ok" : state.PythonExe is not null ? "warn" : "error");

        SetStatus("Python Runtime",
            state.PythonExe is not null ? $"{state.PythonExe} [{state.PythonSource}]" : "Missing",
            state.PythonExe is not null ? "ok" : "error");

        SetStatus("MTGA Install",
            state.MtgaDir is not null ? $"{state.MtgaDir} ({state.MtgaDirSource})" : "Not detected",
            state.MtgaDir is not null ? "ok" : "error");

        SetStatus("MTGA Process",
            state.MtgaRunning ? "Running" : "Not running",
            state.MtgaRunning ? "warn" : "ok");

        SetStatus("BepInEx",
            state.BepinexInstalled ? state.BepinexDir! : "Missing",
            state.BepinexInstalled ? "ok" : "error");

        SetStatus("Bridge Plugin",
            state.PluginInstalled ? state.PluginInstallPath!
            : state.PluginBuilt ? $"Built at {state.PluginBuildPath}"
            : "Missing",
            state.PluginInstalled ? "ok" : state.PluginBuilt ? "warn" : "error");

        SetStatus("BepInEx Bundle",
            state.BepinexBundle is not null ? state.BepinexBundle
            : state.BepinexInstalled ? "Already installed in MTGA"
            : "No bundle found",
            state.BepinexBundle is not null || state.BepinexInstalled ? "ok" : "warn");

        SetStatus("Player.log",
            File.Exists(state.PlayerLog) ? state.PlayerLog : $"Missing ({state.PlayerLog})",
            File.Exists(state.PlayerLog) ? "ok" : "warn");

        SetStatus("Bridge Readiness",
            state.IsFullyProvisioned ? "Ready" : state.IsLaunchable ? "Ready (fallback)" : "Incomplete",
            state.IsFullyProvisioned || state.IsLaunchable ? "ok" : "warn");

        RefreshLogTails();
    }

    private void SetStatus(string key, string text, string level)
    {
        if (!_statusLabels.TryGetValue(key, out var label)) return;
        label.Text = text;
        label.Foreground = level switch
        {
            "ok" => new SolidColorBrush(ColorHelper.FromArgb(255, 36, 92, 60)),
            "warn" => new SolidColorBrush(ColorHelper.FromArgb(255, 138, 90, 0)),
            "error" => new SolidColorBrush(ColorHelper.FromArgb(255, 141, 31, 31)),
            _ => new SolidColorBrush(ColorHelper.FromArgb(255, 51, 78, 104)),
        };
    }

    private void RefreshLogTails()
    {
        var sb = new StringBuilder();
        sb.AppendLine("[standalone.log tail]");
        var standaloneLog = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            ".arenamcp", "standalone.log");
        sb.AppendLine(RuntimeDetector.TailText(standaloneLog, 3000));
        sb.AppendLine();
        sb.AppendLine("[Player.log tail]");
        sb.AppendLine(RuntimeDetector.TailText(_state?.PlayerLog, 3000));
        sb.AppendLine();
        sb.AppendLine("[BepInEx log tail]");
        sb.AppendLine(RuntimeDetector.TailText(_state?.BepinexLog, 3000));
        LogTailText.Text = sb.ToString();
    }

    private string GetSelectedMtgaDir()
    {
        var text = MtgaPathBox.Text?.Trim();
        if (!string.IsNullOrEmpty(text)) return text;
        if (_state?.MtgaDir is not null) return _state.MtgaDir;
        throw new InvalidOperationException("MTGA install folder is not set");
    }

    // ── Button handlers ─────────────────────────────────────────────

    private void RestartCoach_Click(object sender, RoutedEventArgs e)
        => _mainPage?.RestartCoach(false, DryRunCheck.IsChecked == true, AfkCheck.IsChecked == true);

    private void RestartAutopilot_Click(object sender, RoutedEventArgs e)
        => _mainPage?.RestartCoach(true, DryRunCheck.IsChecked == true, AfkCheck.IsChecked == true);

    private void Refresh_Click(object sender, RoutedEventArgs e)
        => _mainPage?.RefreshState();

    private void RefreshLogs_Click(object sender, RoutedEventArgs e)
        => RefreshLogTails();

    private async void Browse_Click(object sender, RoutedEventArgs e)
    {
        var picker = new FolderPicker();
        picker.SuggestedStartLocation = PickerLocationId.ComputerFolder;
        picker.FileTypeFilter.Add("*");
        var app = Application.Current as App;
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(app?.MainWindow);
        WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
        var folder = await picker.PickSingleFolderAsync();
        if (folder is not null) MtgaPathBox.Text = folder.Path;
    }

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        var path = MtgaPathBox.Text?.Trim();
        if (string.IsNullOrEmpty(path)) { await ShowInfo("Choose an MTGA folder first."); return; }
        RuntimeDetector.SetSavedMtgaDir(path);
        _mainPage?.RefreshState();
        await ShowInfo($"Saved MTGA folder:\n{path}");
    }

    private async void ProvisionRuntime_Click(object sender, RoutedEventArgs e)
    {
        try { ProcessLauncher.RunSetupWizard(); }
        catch (Exception ex) { await ShowError(ex.Message); }
    }

    private async void RepairBridge_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var changed = ProcessLauncher.RepairBridgeStack(GetSelectedMtgaDir());
            _mainPage?.RefreshState();
            await ShowInfo(changed.Count > 0 ? string.Join("\n", changed) : "No changes needed.");
        }
        catch (Exception ex) { await ShowError(ex.Message); }
    }

    private async void InstallBepInEx_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var target = ProcessLauncher.InstallBepInEx(GetSelectedMtgaDir());
            _mainPage?.RefreshState();
            await ShowInfo($"BepInEx installed at:\n{target}");
        }
        catch (Exception ex) { await ShowError(ex.Message); }
    }

    private async void InstallPlugin_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var target = ProcessLauncher.InstallPlugin(GetSelectedMtgaDir());
            _mainPage?.RefreshState();
            await ShowInfo($"Plugin installed at:\n{target}");
        }
        catch (Exception ex) { await ShowError(ex.Message); }
    }

    private void OpenMtga_Click(object sender, RoutedEventArgs e)
    { try { ProcessLauncher.OpenPath(GetSelectedMtgaDir()); } catch { } }
    private void OpenPlayerLog_Click(object sender, RoutedEventArgs e)
    { if (_state?.PlayerLog is not null) ProcessLauncher.OpenPath(_state.PlayerLog); }
    private void OpenBepInExLog_Click(object sender, RoutedEventArgs e)
    { if (_state?.BepinexLog is not null) ProcessLauncher.OpenPath(_state.BepinexLog); }
    private void OpenReleases_Click(object sender, RoutedEventArgs e)
        => ProcessLauncher.OpenUrl(GitHubReleases);

    private async Task ShowInfo(string msg)
    {
        await new ContentDialog { Title = "mtgacoach", Content = msg, CloseButtonText = "OK", XamlRoot = this.XamlRoot }.ShowAsync();
    }
    private async Task ShowError(string msg)
    {
        await new ContentDialog { Title = "Error", Content = msg, CloseButtonText = "OK", XamlRoot = this.XamlRoot }.ShowAsync();
    }
}
