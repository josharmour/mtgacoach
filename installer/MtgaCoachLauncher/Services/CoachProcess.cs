using System.Diagnostics;
using System.Text.Json;

namespace MtgaCoachLauncher.Services;

public sealed class CoachProcess : IDisposable
{
    private Process? _process;
    private Thread? _readerThread;
    private Thread? _stderrThread;
    private bool _running;

    public event Action<JsonElement>? EventReceived;
    public event Action<string>? StderrLine;
    public event Action<int>? Exited;

    public bool IsRunning => _running && _process is not null && !_process.HasExited;
    public string? LastError { get; private set; }

    public void Start(bool autopilot = false, bool dryRun = false, bool afk = false)
    {
        if (_running) return;

        var appRoot = RuntimeDetector.GetAppRoot();
        var runtimeRoot = RuntimeDetector.GetRuntimeRoot();
        var (pythonExe, pythonSource) = RuntimeDetector.FindPythonExecutable();

        if (pythonExe is null)
            throw new InvalidOperationException("Python executable not found");

        // -u forces unbuffered stdout/stderr (critical for pipe communication)
        var args = new List<string> { "-u", "-m", "arenamcp.standalone", "--pipe" };
        if (autopilot) args.Add("--autopilot");
        if (dryRun) args.Add("--dry-run");
        if (afk) args.Add("--afk");

        var srcDir = Path.Combine(appRoot, "src");

        LastError = $"Launching: {pythonExe} ({pythonSource})\nArgs: {string.Join(" ", args)}\nWorkDir: {appRoot}\nPYTHONPATH: {srcDir}";
        CrashLogger.LogBreadcrumb(
            $"CoachProcess.Start python={pythonExe} source={pythonSource} workdir={appRoot} pythonpath={srcDir}");

        var psi = new ProcessStartInfo(pythonExe, string.Join(" ", args))
        {
            WorkingDirectory = appRoot,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardInput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };
        psi.Environment["PYTHONPATH"] = srcDir;
        psi.Environment["MTGACOACH_RUNTIME_ROOT"] = runtimeRoot;
        psi.Environment["MTGACOACH_FRONTEND"] = "winui";
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";

        _process = Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start Python coach process");
        CrashLogger.LogBreadcrumb($"CoachProcess started pid={_process.Id}");

        _running = true;

        _readerThread = new Thread(ReadLoop) { IsBackground = true, Name = "CoachStdout" };
        _readerThread.Start();

        _stderrThread = new Thread(StderrLoop) { IsBackground = true, Name = "CoachStderr" };
        _stderrThread.Start();
    }

    public void SendCommand(string cmd, string? text = null)
    {
        if (_process?.HasExited != false) return;
        var obj = new Dictionary<string, string> { ["cmd"] = cmd };
        if (text is not null) obj["text"] = text;
        try
        {
            var line = JsonSerializer.Serialize(obj);
            _process.StandardInput.WriteLine(line);
            _process.StandardInput.Flush();
        }
        catch { }
    }

    public void Stop()
    {
        _running = false;
        if (_process is not null && !_process.HasExited)
        {
            CrashLogger.LogBreadcrumb($"CoachProcess stopping pid={_process.Id}");
            try
            {
                _process.StandardInput.Close();
                if (!_process.WaitForExit(3000))
                    _process.Kill();
            }
            catch { }
        }
    }

    public void Dispose()
    {
        Stop();
        _process?.Dispose();
    }

    private void ReadLoop()
    {
        try
        {
            while (_running && _process?.HasExited == false)
            {
                var line = _process.StandardOutput.ReadLine();
                if (line is null) break;
                line = line.Trim();
                if (line.Length == 0) continue;
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    EventReceived?.Invoke(doc.RootElement.Clone());
                }
                catch (JsonException)
                {
                    // Non-JSON output — surface it as a log event
                    var fallback = JsonDocument.Parse(
                        $"{{\"type\":\"log\",\"message\":\"[raw] {line.Replace("\"", "\\\"").Replace("\\", "\\\\")}\"}}");
                    EventReceived?.Invoke(fallback.RootElement.Clone());
                }
            }
        }
        catch { }
        finally
        {
            _running = false;
            var exitCode = -1;
            try { exitCode = _process?.ExitCode ?? -1; } catch { }
            CrashLogger.LogBreadcrumb($"CoachProcess exited code={exitCode}");
            Exited?.Invoke(exitCode);
        }
    }

    private void StderrLoop()
    {
        try
        {
            while (_running && _process?.HasExited == false)
            {
                var line = _process.StandardError.ReadLine();
                if (line is null) break;
                LastError = line;
                StderrLine?.Invoke(line);
            }
        }
        catch { }
    }
}
