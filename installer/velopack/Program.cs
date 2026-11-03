using System.Diagnostics;
using System.Text.Json;
using Velopack;

// Velopack handles install/update hooks before creating a GUI or taking locks.
VelopackApp.Build().SetAutoApplyOnStartup(false).Run();
var root = AppContext.BaseDirectory;
var config = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "coach-release.json"))).RootElement;
var channel = config.GetProperty("channel").GetString()!;
var compatibility = "coach-runtime-" + config.GetProperty("compatibility").GetString();
var lane = channel.StartsWith("master-") ? "master" : "stable";
var python = OperatingSystem.IsWindows() ? "runtime/pythonw.exe" : "runtime/bin/python3";
var start = new ProcessStartInfo(Path.Combine(root, python)) { UseShellExecute = false, WorkingDirectory = root };
foreach (var arg in new[] { "-I", "-X", "utf8", "-m", "arenamcp.desktop" }.Concat(args))
    start.ArgumentList.Add(arg);
start.Environment["MTGACOACH_APP_ROOT"] = root;
var state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "mtgacoach", "updates", channel);
Directory.CreateDirectory(state);
void Log(string message) {
    try { File.AppendAllText(Path.Combine(state, "update.log"), $"{DateTimeOffset.Now:O} {message}\n"); }
    catch (IOException) { }
}
var manager = new UpdateManager($"https://github.com/josharmour/mtgacoach/releases/download/coach-{lane}-feed/");
if (!manager.IsInstalled || args.Length > 0 || Environment.GetEnvironmentVariable("MTGACOACH_NO_UPDATE_CHECK") is not null)
    return RunDesktop(start);

// One host owns the pending update and the child lifecycle.
FileStream hostLock;
try { hostLock = new FileStream(Path.Combine(state, "host.lock"), FileMode.OpenOrCreate, FileAccess.Write, FileShare.None); }
catch (IOException) { return 0; }
using var owner = hostLock;
start.Environment["MTGACOACH_BUILD_LABEL"] = $"{channel} {manager.CurrentVersion} ({config.GetProperty("commit").GetString()![..8]})";
var marker = Path.Combine(state, "pending.json");
bool CanAuto(VelopackAsset asset) => asset.NotesMarkdown?.Split('\n').Any(line => line.Trim() == compatibility) == true;
var pending = manager.UpdatePendingRestart;
if (pending != null && CanAuto(pending)) {
    manager.WaitExitThenApplyUpdates(pending, silent: true, restart: true);
    return 0;
}
void Announce(VelopackAsset asset) {
    var json = JsonSerializer.Serialize(new { version = asset.Version.ToString(), automatic = CanAuto(asset) });
    File.WriteAllText(marker + ".tmp", json);
    File.Move(marker + ".tmp", marker, overwrite: true);
}
File.Delete(marker);
if (pending != null) Announce(pending);
start.Environment["MTGACOACH_VELOPACK_PENDING"] = marker;
using var cancel = new CancellationTokenSource();
var worker = Task.Run(async () => {
    try {
        var update = await manager.CheckForUpdatesAsync();
        if (update != null && !cancel.IsCancellationRequested) {
            Log($"Downloading {update.TargetFullRelease.Version}");
            await manager.DownloadUpdatesAsync(update, cancelToken: cancel.Token);
            Announce(update.TargetFullRelease);
        }
    } catch (Exception ex) { Log(ex.Message); }
});
int code;
do { code = RunDesktop(start); } while (code == 42);
cancel.Cancel();
await worker;
if ((code == 86 || code == 87) && manager.UpdatePendingRestart is { } asset && (code == 87 || CanAuto(asset))) {
    manager.WaitExitThenApplyUpdates(asset, silent: true, restart: true);
    return 0;
}
return code;

static int RunDesktop(ProcessStartInfo start) {
    using var child = Process.Start(start) ?? throw new IOException("Could not start Coach");
    child.WaitForExit();
    return child.ExitCode;
}
