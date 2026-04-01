using System.Diagnostics;
using System.Text.Json;
using Microsoft.Win32;
using MtgaCoachLauncher.Models;

namespace MtgaCoachLauncher.Services;

/// <summary>
/// C# port of windows_integration.py detection logic.
/// </summary>
public static class RuntimeDetector
{
    private static readonly string SettingsDir =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".arenamcp");

    private static readonly string SettingsFile =
        Path.Combine(SettingsDir, "settings.json");

    private static readonly string[] CommonMtgaPaths =
    [
        @"C:\Program Files\Wizards of the Coast\MTGA",
        @"C:\Program Files (x86)\Wizards of the Coast\MTGA",
        @"D:\Program Files\Wizards of the Coast\MTGA",
        @"C:\Program Files\Epic Games\MagicTheGathering",
    ];

    public static string GetAppRoot()
    {
        // Walk up from the exe to find pyproject.toml
        var dir = AppContext.BaseDirectory;
        for (int i = 0; i < 6; i++)
        {
            if (File.Exists(Path.Combine(dir, "pyproject.toml")))
                return dir;
            var parent = Directory.GetParent(dir);
            if (parent is null) break;
            dir = parent.FullName;
        }
        return AppContext.BaseDirectory;
    }

    public static string GetRuntimeRoot()
    {
        var envVal = Environment.GetEnvironmentVariable("MTGACOACH_RUNTIME_ROOT");
        if (!string.IsNullOrEmpty(envVal))
            return envVal;

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(localAppData, "mtgacoach");
    }

    public static (string? exe, string source) FindPythonExecutable()
    {
        var runtimeRoot = GetRuntimeRoot();
        var appRoot = GetAppRoot();

        // 1. Runtime venv
        var runtimeVenv = Path.Combine(runtimeRoot, "venv", "Scripts", "python.exe");
        if (File.Exists(runtimeVenv))
            return (runtimeVenv, "runtime_venv");

        // 2. App venv (repo .venv)
        var appVenv = Path.Combine(appRoot, ".venv", "Scripts", "python.exe");
        if (File.Exists(appVenv))
            return (appVenv, "app_venv");

        // 3. PATH
        try
        {
            var psi = new ProcessStartInfo("where", "python")
            {
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var proc = Process.Start(psi);
            if (proc is not null)
            {
                var output = proc.StandardOutput.ReadToEnd().Trim();
                proc.WaitForExit(3000);
                var first = output.Split('\n', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault()?.Trim();
                if (!string.IsNullOrEmpty(first) && File.Exists(first))
                    return (first, "PATH");
            }
        }
        catch { }

        return (null, "not_found");
    }

    public static (string? dir, string source) FindMtgaInstallDir()
    {
        // 1. Saved settings
        var saved = GetSavedMtgaDir();
        if (saved is not null && Directory.Exists(saved))
            return (saved, "settings");

        // 2. Environment variable
        var envVal = Environment.GetEnvironmentVariable("MTGA_DIR");
        if (!string.IsNullOrEmpty(envVal) && Directory.Exists(envVal))
            return (envVal, "environment");

        // 3. Registry uninstall keys
        var regPath = FindMtgaFromRegistry();
        if (regPath is not null)
            return (regPath, "registry");

        // 4. Common paths
        foreach (var path in CommonMtgaPaths)
        {
            if (Directory.Exists(path))
                return (path, "common_path");
        }

        return (null, "not_found");
    }

    private static string? FindMtgaFromRegistry()
    {
        string[] uninstallKeys =
        [
            @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            @"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ];

        foreach (var keyPath in uninstallKeys)
        {
            try
            {
                using var key = Registry.LocalMachine.OpenSubKey(keyPath);
                if (key is null) continue;

                foreach (var subKeyName in key.GetSubKeyNames())
                {
                    try
                    {
                        using var subKey = key.OpenSubKey(subKeyName);
                        var displayName = subKey?.GetValue("DisplayName")?.ToString();
                        if (displayName is null) continue;
                        if (!displayName.Contains("Magic", StringComparison.OrdinalIgnoreCase) ||
                            !displayName.Contains("Gathering", StringComparison.OrdinalIgnoreCase))
                            continue;

                        var installLocation = subKey?.GetValue("InstallLocation")?.ToString();
                        if (!string.IsNullOrEmpty(installLocation) && Directory.Exists(installLocation))
                            return installLocation;
                    }
                    catch { }
                }
            }
            catch { }
        }
        return null;
    }

    public static bool IsMtgaRunning()
    {
        try
        {
            return Process.GetProcessesByName("MTGA").Length > 0;
        }
        catch
        {
            return false;
        }
    }

    public static string? GetSavedMtgaDir()
    {
        try
        {
            if (!File.Exists(SettingsFile)) return null;
            var json = File.ReadAllText(SettingsFile);
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("mtga_install_dir", out var prop))
            {
                var val = prop.GetString();
                if (!string.IsNullOrEmpty(val) && Directory.Exists(val))
                    return val;
            }
        }
        catch { }
        return null;
    }

    public static void SetSavedMtgaDir(string path)
    {
        Directory.CreateDirectory(SettingsDir);
        Dictionary<string, object> data = [];
        try
        {
            if (File.Exists(SettingsFile))
            {
                var existing = JsonSerializer.Deserialize<Dictionary<string, object>>(
                    File.ReadAllText(SettingsFile));
                if (existing is not null) data = existing;
            }
        }
        catch { }
        data["mtga_install_dir"] = path;
        File.WriteAllText(SettingsFile,
            JsonSerializer.Serialize(data, new JsonSerializerOptions { WriteIndented = true }));
    }

    public static RuntimeState DetectRuntimeState()
    {
        var appRoot = GetAppRoot();
        var runtimeRoot = GetRuntimeRoot();
        var runtimeVenvDir = Path.Combine(runtimeRoot, "venv");
        var runtimeVenvExists = File.Exists(Path.Combine(runtimeVenvDir, "Scripts", "python.exe"));
        var (pythonExe, pythonSource) = FindPythonExecutable();
        var (mtgaDir, mtgaDirSource) = FindMtgaInstallDir();
        var mtgaRunning = IsMtgaRunning();

        // Player.log
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        // MTGA uses LocalLow
        var playerLog = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "AppData", "LocalLow", "Wizards Of The Coast", "MTGA", "Player.log");

        // BepInEx paths
        string? bepinexDir = null;
        bool bepinexInstalled = false;
        string? bepinexLog = null;
        string? pluginInstallPath = null;
        bool pluginInstalled = false;

        if (mtgaDir is not null)
        {
            var bDir = Path.Combine(mtgaDir, "BepInEx");
            var coreDll = Path.Combine(bDir, "core", "BepInEx.dll");
            if (Directory.Exists(bDir) && File.Exists(coreDll))
            {
                bepinexDir = bDir;
                bepinexInstalled = true;
            }
            bepinexLog = Path.Combine(bDir, "LogOutput.log");

            var pluginPath = Path.Combine(bDir, "plugins", "MtgaCoachBridge.dll");
            if (File.Exists(pluginPath))
            {
                pluginInstallPath = pluginPath;
                pluginInstalled = true;
            }
        }

        // Plugin build output
        var pluginBuildPath = Path.Combine(appRoot, "bepinex-plugin", "MtgaCoachBridge",
            "bin", "Release", "net472", "MtgaCoachBridge.dll");
        var pluginBuilt = File.Exists(pluginBuildPath);

        // BepInEx bundle
        string? bepinexBundle = null;
        string[] bundleSearchDirs = ["third_party", "assets"];
        foreach (var sub in bundleSearchDirs)
        {
            var bundleDir = Path.Combine(appRoot, sub, "BepInEx");
            if (Directory.Exists(bundleDir))
            {
                bepinexBundle = bundleDir;
                break;
            }
            // Also check for zip
            var bundleZip = Path.Combine(appRoot, sub, "BepInEx.zip");
            if (File.Exists(bundleZip))
            {
                bepinexBundle = bundleZip;
                break;
            }
        }

        // Issues
        List<string> issues = [];
        if (pythonExe is null)
            issues.Add("Python 3.10+ not found");
        if (mtgaDir is null)
            issues.Add("MTGA install not detected");
        if (mtgaDir is not null && !bepinexInstalled)
            issues.Add("BepInEx not installed in MTGA");
        if (mtgaDir is not null && bepinexInstalled && !pluginInstalled)
            issues.Add("MtgaCoachBridge.dll not deployed to BepInEx/plugins");

        return new RuntimeState
        {
            RepoDir = appRoot,
            RuntimeRoot = runtimeRoot,
            RuntimeVenvDir = runtimeVenvDir,
            RuntimeVenvExists = runtimeVenvExists,
            PythonExe = pythonExe,
            PythonSource = pythonSource,
            MtgaDir = mtgaDir,
            MtgaDirSource = mtgaDirSource,
            MtgaRunning = mtgaRunning,
            PlayerLog = playerLog,
            BepinexLog = bepinexLog,
            BepinexDir = bepinexDir,
            BepinexInstalled = bepinexInstalled,
            PluginInstallPath = pluginInstallPath,
            PluginInstalled = pluginInstalled,
            PluginBuildPath = pluginBuilt ? pluginBuildPath : null,
            PluginBuilt = pluginBuilt,
            BepinexBundle = bepinexBundle,
            Issues = issues,
        };
    }

    public static string TailText(string? path, int maxBytes = 8192)
    {
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
            return "";
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var length = fs.Length;
            if (length == 0) return "";
            var offset = Math.Max(0, length - maxBytes);
            fs.Seek(offset, SeekOrigin.Begin);
            var buffer = new byte[Math.Min(maxBytes, length)];
            var read = fs.Read(buffer, 0, buffer.Length);
            return System.Text.Encoding.UTF8.GetString(buffer, 0, read);
        }
        catch
        {
            return "";
        }
    }

    public static string ReadVersion()
    {
        var pyproject = Path.Combine(GetAppRoot(), "pyproject.toml");
        try
        {
            foreach (var line in File.ReadAllLines(pyproject))
            {
                var trimmed = line.Trim();
                if (trimmed.StartsWith("version = "))
                {
                    var parts = trimmed.Split('"');
                    if (parts.Length >= 2) return parts[1];
                }
            }
        }
        catch { }
        return "unknown";
    }
}
