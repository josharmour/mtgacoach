using System.Net.Http;
using System.Text;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Xaml.Media;
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

    public void AttachMainPage(MainPage mainPage)
    {
        _mainPage = mainPage;
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

    // ── Fix Everything ────────────────────────────────────────────

    private async void FixEverything_Click(object sender, RoutedEventArgs e)
    {
        BtnFixAll.IsEnabled = false;
        FixProgress.IsActive = true;
        FixLog.Visibility = Microsoft.UI.Xaml.Visibility.Visible;
        var log = new List<string>();

        void SetStep(string msg)
        {
            FixStatus.Text = msg;
            DispatcherQueue.TryEnqueue(() => { });
        }

        void Log(string msg)
        {
            log.Add(msg);
            FixLog.Text = string.Join("\n", log);
        }

        try
        {
            // ── 1. Detect current state ─────────────────────────────
            SetStep("Scanning...");
            var state = RuntimeDetector.DetectRuntimeState();
            var mtgaDir = state.MtgaDir ?? GetSelectedMtgaDir();

            if (state.Issues.Count == 0 && state.IsFullyProvisioned)
            {
                Log("[ok] System is fully provisioned — nothing to fix");
                SetStep("All good");
                return;
            }

            Log($"[..] Found {state.Issues.Count} issue(s): {string.Join(", ", state.Issues)}");

            // ── 2. Python / venv ────────────────────────────────────
            if (state.PythonExe is null)
            {
                Log("[!!] No Python found — cannot auto-fix. Use 'Provision Runtime' manually.");
                SetStep("Blocked: no Python");
                return;
            }

            if (!state.RuntimeVenvExists)
            {
                SetStep("Provisioning Python venv...");
                Log("[..] Creating runtime venv...");
                var wizard = ProcessLauncher.RunSetupWizard();
                await Task.Run(() => wizard.WaitForExit(120_000));
                state = RuntimeDetector.DetectRuntimeState();

                if (state.RuntimeVenvExists)
                    Log("[ok] Runtime venv created");
                else
                    Log("[!!] Venv setup may still be running — continue anyway");
            }
            else
            {
                Log("[ok] Python venv already exists");
            }

            // ── 3. MTGA location ────────────────────────────────────
            if (state.MtgaDir is null)
            {
                Log("[!!] MTGA install not detected — set the path above and retry");
                SetStep("Blocked: no MTGA path");
                return;
            }
            else
            {
                Log($"[ok] MTGA at {state.MtgaDir}");
            }

            // ── 4. MTGA running check ───────────────────────────────
            if (RuntimeDetector.IsMtgaRunning())
            {
                SetStep("Waiting for MTGA to close...");
                Log("[..] MTGA is running — close it to continue bridge repair");
                var result = await new ContentDialog
                {
                    Title = "MTGA is running",
                    Content = "BepInEx and the bridge plugin can only be installed while MTGA is closed.\n\nClose MTGA and click Retry, or Skip to finish without bridge repair.",
                    PrimaryButtonText = "Retry",
                    SecondaryButtonText = "Skip",
                    CloseButtonText = "Cancel",
                    XamlRoot = this.XamlRoot,
                }.ShowAsync();

                if (result == ContentDialogResult.Secondary)
                {
                    Log("[--] Skipped bridge repair (MTGA still running)");
                    goto Done;
                }
                if (result == ContentDialogResult.None)
                {
                    Log("[--] Cancelled");
                    SetStep("Cancelled");
                    return;
                }

                // User clicked Retry — recheck
                if (RuntimeDetector.IsMtgaRunning())
                {
                    Log("[!!] MTGA is still running — skipping bridge repair");
                    goto Done;
                }
            }

            // ── 5. BepInEx ──────────────────────────────────────────
            state = RuntimeDetector.DetectRuntimeState();
            if (!state.BepinexInstalled)
            {
                if (state.BepinexBundle is null)
                {
                    Log("[!!] No BepInEx bundle found in assets/ or third_party/ — cannot install");
                }
                else
                {
                    SetStep("Installing BepInEx...");
                    var target = ProcessLauncher.InstallBepInEx(mtgaDir);
                    Log($"[ok] BepInEx installed at {target}");
                }
            }
            else
            {
                Log("[ok] BepInEx already installed");
            }

            // ── 6. Bridge plugin ────────────────────────────────────
            state = RuntimeDetector.DetectRuntimeState();
            if (!state.PluginInstalled)
            {
                if (!state.PluginBuilt)
                {
                    Log("[!!] Plugin DLL not built — build it first (dotnet build -c Release)");
                }
                else
                {
                    SetStep("Installing bridge plugin...");
                    var target = ProcessLauncher.InstallPlugin(mtgaDir);
                    Log($"[ok] Plugin installed at {target}");
                }
            }
            else
            {
                // Even if installed, re-copy if a newer build exists
                if (state.PluginBuilt && state.PluginBuildPath is not null && state.PluginInstallPath is not null)
                {
                    var buildTime = File.GetLastWriteTimeUtc(state.PluginBuildPath);
                    var installTime = File.GetLastWriteTimeUtc(state.PluginInstallPath);
                    if (buildTime > installTime)
                    {
                        SetStep("Updating bridge plugin...");
                        var target = ProcessLauncher.InstallPlugin(mtgaDir);
                        Log($"[ok] Plugin updated (build was newer)");
                    }
                    else
                    {
                        Log("[ok] Bridge plugin already installed and up to date");
                    }
                }
                else
                {
                    Log("[ok] Bridge plugin already installed");
                }
            }

            // ── 7. Version sync check ───────────────────────────────
            SetStep("Checking version sync...");
            try
            {
                using var http = new HttpClient();
                http.DefaultRequestHeaders.UserAgent.ParseAdd("mtgacoach-launcher/1.0");
                var json = await http.GetStringAsync(
                    "https://api.github.com/repos/josharmour/mtgacoach/releases/latest");
                using var doc = JsonDocument.Parse(json);
                var latestTag = doc.RootElement.GetProperty("tag_name").GetString() ?? "";
                var currentVersion = "v" + RuntimeDetector.ReadVersion();

                if (latestTag == currentVersion)
                    Log($"[ok] Version {currentVersion} matches latest release");
                else
                    Log($"[!!] Version mismatch: installed {currentVersion}, latest {latestTag} — use Check for Updates");
            }
            catch
            {
                Log("[--] Could not check version (offline?)");
            }

        Done:
            // ── Final state refresh ─────────────────────────────────
            _mainPage?.RefreshState();
            state = RuntimeDetector.DetectRuntimeState();
            if (state.IsFullyProvisioned)
            {
                SetStep("All fixed");
                Log("\n[ok] System is fully provisioned and ready to launch");
            }
            else if (state.IsLaunchable)
            {
                SetStep("Launchable (with caveats)");
                Log($"\n[ok] System is launchable. Remaining: {string.Join(", ", state.Issues)}");
            }
            else
            {
                SetStep("Some issues remain");
                Log($"\n[!!] Still not launchable. Issues: {string.Join(", ", state.Issues)}");
            }
        }
        catch (Exception ex)
        {
            SetStep("Error");
            Log($"\n[!!] Fix failed: {ex.Message}");
        }
        finally
        {
            BtnFixAll.IsEnabled = true;
            FixProgress.IsActive = false;
        }
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

    private async void CheckUpdate_Click(object sender, RoutedEventArgs e)
    {
        BtnUpdate.IsEnabled = false;
        UpdateStatus.Text = "Checking...";

        try
        {
            using var http = new HttpClient();
            http.DefaultRequestHeaders.UserAgent.ParseAdd("mtgacoach-launcher/1.0");
            var json = await http.GetStringAsync(
                "https://api.github.com/repos/josharmour/mtgacoach/releases/latest");

            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var latestTag = root.GetProperty("tag_name").GetString() ?? "";
            var currentVersion = "v" + RuntimeDetector.ReadVersion();

            if (latestTag == currentVersion)
            {
                UpdateStatus.Text = $"Up to date ({currentVersion})";
                BtnUpdate.IsEnabled = true;
                return;
            }

            UpdateStatus.Text = $"New version available: {latestTag} (you have {currentVersion})";

            // Find the installer asset
            string? downloadUrl = null;
            if (root.TryGetProperty("assets", out var assets))
            {
                foreach (var asset in assets.EnumerateArray())
                {
                    var name = asset.GetProperty("name").GetString() ?? "";
                    if (name.Contains("Setup") && name.EndsWith(".exe"))
                    {
                        downloadUrl = asset.GetProperty("browser_download_url").GetString();
                        break;
                    }
                }
            }

            if (downloadUrl is null)
            {
                UpdateStatus.Text = $"{latestTag} available — no installer found in release";
                BtnUpdate.IsEnabled = true;
                return;
            }

            var result = await new ContentDialog
            {
                Title = "Update Available",
                Content = $"Download {latestTag}?\n\nCurrent: {currentVersion}",
                PrimaryButtonText = "Download & Install",
                CloseButtonText = "Later",
                XamlRoot = this.XamlRoot,
            }.ShowAsync();

            if (result != ContentDialogResult.Primary)
            {
                BtnUpdate.IsEnabled = true;
                return;
            }

            UpdateStatus.Text = "Downloading...";
            var tempFile = Path.Combine(Path.GetTempPath(), "mtgacoach-Setup.exe");
            var bytes = await http.GetByteArrayAsync(downloadUrl);
            await File.WriteAllBytesAsync(tempFile, bytes);

            UpdateStatus.Text = "Launching installer...";
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(tempFile)
            {
                UseShellExecute = true
            });

            // Exit the app so the installer can overwrite files
            Application.Current.Exit();
        }
        catch (Exception ex)
        {
            UpdateStatus.Text = $"Update check failed: {ex.Message}";
        }
        finally
        {
            BtnUpdate.IsEnabled = true;
        }
    }

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
