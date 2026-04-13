using System.Runtime.InteropServices;

namespace MtgaCoachLauncher.Services;

public static class AudioPlayback
{
    private const uint SndAsync = 0x00000001;
    private const uint SndNodefault = 0x00000002;
    private const uint SndPurge = 0x00000040;
    private const uint SndFilename = 0x00020000;
    private const uint SndSystem = 0x00200000;
    private static readonly object _sync = new();

    [DllImport("winmm.dll", EntryPoint = "PlaySoundW", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool PlaySound(string? pszSound, nint hmod, uint fdwSound);

    public static bool PlayFile(string path, string? text = null)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            CrashLogger.LogBreadcrumb("AudioPlayback.PlayFile skipped: empty path");
            return false;
        }

        var fullPath = Path.GetFullPath(path);
        if (!File.Exists(fullPath))
        {
            CrashLogger.LogBreadcrumb($"AudioPlayback.PlayFile missing file: {fullPath}");
            return false;
        }

        lock (_sync)
        {
            StopCore();

            var ok = PlaySound(fullPath, 0, SndFilename | SndAsync | SndNodefault | SndSystem);
            var lastError = Marshal.GetLastWin32Error();
            CrashLogger.LogBreadcrumb(
                $"AudioPlayback.PlayFile path={fullPath} ok={ok} lastError={lastError} text={Truncate(text, 200)}");
            return ok;
        }
    }

    public static void Stop()
    {
        lock (_sync)
        {
            StopCore();
        }
    }

    private static void StopCore()
    {
        try
        {
            PlaySound(null, 0, SndPurge);
        }
        catch (Exception ex)
        {
            CrashLogger.LogException("AudioPlayback.Stop", ex);
        }
    }

    private static string Truncate(string? text, int maxLength)
    {
        if (string.IsNullOrEmpty(text) || text.Length <= maxLength)
            return text ?? "";

        return text[..maxLength] + "...";
    }
}
