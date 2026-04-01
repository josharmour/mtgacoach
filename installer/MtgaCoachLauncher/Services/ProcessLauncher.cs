using System.Diagnostics;
using System.IO.Compression;

namespace MtgaCoachLauncher.Services;

/// <summary>
/// Launches Python subprocesses and handles repair operations.
/// Mirrors windows_integration.py launch/repair functions.
/// </summary>
public static class ProcessLauncher
{
    public static Process LaunchMode(bool autopilot, bool dryRun, bool afk)
    {
        var appRoot = RuntimeDetector.GetAppRoot();
        var runtimeRoot = RuntimeDetector.GetRuntimeRoot();
        var (pythonExe, _) = RuntimeDetector.FindPythonExecutable();

        if (pythonExe is null)
            throw new InvalidOperationException("Python executable not found");

        var args = new List<string> { "launcher.py" };
        if (autopilot) args.Add("--autopilot");
        if (dryRun) args.Add("--dry-run");
        if (afk) args.Add("--afk");

        // Build a cmd.exe invocation that sets env vars and launches Python in a new console
        var srcDir = Path.Combine(appRoot, "src");
        var envPrefix = $"set \"PYTHONPATH={srcDir}\" && set \"MTGACOACH_RUNTIME_ROOT={runtimeRoot}\" && ";
        var fullCmd = $"{envPrefix}\"{pythonExe}\" {string.Join(" ", args)}";

        var psi = new ProcessStartInfo("cmd.exe", $"/c {fullCmd}")
        {
            WorkingDirectory = appRoot,
            UseShellExecute = true,
        };

        return Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start process");
    }

    public static Process RunSetupWizard()
    {
        var appRoot = RuntimeDetector.GetAppRoot();
        var runtimeRoot = RuntimeDetector.GetRuntimeRoot();
        var (pythonExe, _) = RuntimeDetector.FindPythonExecutable();

        if (pythonExe is null)
            throw new InvalidOperationException("Python executable not found");

        var fullCmd = $"set \"MTGACOACH_RUNTIME_ROOT={runtimeRoot}\" && \"{pythonExe}\" setup_wizard.py";

        var psi = new ProcessStartInfo("cmd.exe", $"/c {fullCmd}")
        {
            WorkingDirectory = appRoot,
            UseShellExecute = true,
        };

        return Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start setup wizard");
    }

    public static string InstallBepInEx(string mtgaDir)
    {
        if (RuntimeDetector.IsMtgaRunning())
            throw new InvalidOperationException("Close MTGA before installing BepInEx");

        var state = RuntimeDetector.DetectRuntimeState();
        var bundle = state.BepinexBundle
            ?? throw new FileNotFoundException("No BepInEx bundle found in assets/ or third_party/");

        var targetDir = Path.Combine(mtgaDir, "BepInEx");

        if (bundle.EndsWith(".zip", StringComparison.OrdinalIgnoreCase))
        {
            ZipFile.ExtractToDirectory(bundle, mtgaDir, overwriteFiles: true);
        }
        else if (Directory.Exists(bundle))
        {
            CopyDirectory(bundle, targetDir);
        }

        return targetDir;
    }

    public static string InstallPlugin(string mtgaDir)
    {
        if (RuntimeDetector.IsMtgaRunning())
            throw new InvalidOperationException("Close MTGA before installing the plugin");

        var appRoot = RuntimeDetector.GetAppRoot();
        var srcDll = Path.Combine(appRoot, "bepinex-plugin", "MtgaCoachBridge",
            "bin", "Release", "net472", "MtgaCoachBridge.dll");

        if (!File.Exists(srcDll))
            throw new FileNotFoundException($"Plugin DLL not found at {srcDll}");

        var pluginsDir = Path.Combine(mtgaDir, "BepInEx", "plugins");
        Directory.CreateDirectory(pluginsDir);
        var destDll = Path.Combine(pluginsDir, "MtgaCoachBridge.dll");
        File.Copy(srcDll, destDll, overwrite: true);
        return destDll;
    }

    public static List<string> RepairBridgeStack(string mtgaDir)
    {
        var changed = new List<string>();

        var bepinexCore = Path.Combine(mtgaDir, "BepInEx", "core", "BepInEx.dll");
        if (!File.Exists(bepinexCore))
        {
            changed.Add(InstallBepInEx(mtgaDir));
        }

        var pluginDll = Path.Combine(mtgaDir, "BepInEx", "plugins", "MtgaCoachBridge.dll");
        if (!File.Exists(pluginDll))
        {
            changed.Add(InstallPlugin(mtgaDir));
        }

        return changed;
    }

    public static void OpenPath(string path)
    {
        if (File.Exists(path) || Directory.Exists(path))
        {
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
    }

    public static void OpenUrl(string url)
    {
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    private static void CopyDirectory(string source, string dest)
    {
        Directory.CreateDirectory(dest);
        foreach (var file in Directory.GetFiles(source))
        {
            File.Copy(file, Path.Combine(dest, Path.GetFileName(file)), overwrite: true);
        }
        foreach (var dir in Directory.GetDirectories(source))
        {
            CopyDirectory(dir, Path.Combine(dest, Path.GetFileName(dir)));
        }
    }
}
