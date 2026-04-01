namespace MtgaCoachLauncher.Models;

/// <summary>
/// Mirrors the Python RuntimeState dataclass from windows_integration.py.
/// </summary>
public sealed class RuntimeState
{
    public string RepoDir { get; init; } = "";
    public string RuntimeRoot { get; init; } = "";
    public string RuntimeVenvDir { get; init; } = "";
    public bool RuntimeVenvExists { get; init; }
    public string? PythonExe { get; init; }
    public string PythonSource { get; init; } = "";
    public string? MtgaDir { get; init; }
    public string MtgaDirSource { get; init; } = "";
    public bool MtgaRunning { get; init; }
    public string PlayerLog { get; init; } = "";
    public string? BepinexLog { get; init; }
    public string? BepinexDir { get; init; }
    public bool BepinexInstalled { get; init; }
    public string? PluginInstallPath { get; init; }
    public bool PluginInstalled { get; init; }
    public string? PluginBuildPath { get; init; }
    public bool PluginBuilt { get; init; }
    public string? BepinexBundle { get; init; }
    public List<string> Issues { get; init; } = [];

    public bool IsLaunchable =>
        PythonExe is not null
        && MtgaDir is not null
        && BepinexInstalled
        && PluginInstalled;

    public bool IsFullyProvisioned =>
        IsLaunchable && RuntimeVenvExists;
}
