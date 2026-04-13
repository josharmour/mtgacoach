using System.Runtime.InteropServices;
using System.Text;

namespace MtgaCoachLauncher.Services;

public static class CrashLogger
{
    private const int ExceptionContinueSearch = 0;
    private const uint MiniDumpNormal = 0x00000000;
    private static readonly object _sync = new();
    private static readonly string _logPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "mtgacoach",
        "crash.log");
    private static readonly string _dumpDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "mtgacoach",
        "crashdumps");

    private static UnhandledExceptionFilterDelegate? _nativeFilter;
    private static bool _initialized;

    [DllImport("kernel32.dll")]
    private static extern IntPtr SetUnhandledExceptionFilter(UnhandledExceptionFilterDelegate lpTopLevelExceptionFilter);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern int GetCurrentProcessId();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern int GetCurrentThreadId();

    [DllImport("dbghelp.dll", SetLastError = true)]
    private static extern bool MiniDumpWriteDump(
        IntPtr hProcess,
        int processId,
        IntPtr hFile,
        uint dumpType,
        IntPtr exceptionParam,
        IntPtr userStreamParam,
        IntPtr callbackParam);

    public static string LogPath => _logPath;

    public static void Initialize()
    {
        if (_initialized)
        {
            return;
        }

        lock (_sync)
        {
            if (_initialized)
            {
                return;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
            Directory.CreateDirectory(_dumpDir);

            AppDomain.CurrentDomain.UnhandledException += CurrentDomain_UnhandledException;
            TaskScheduler.UnobservedTaskException += TaskScheduler_UnobservedTaskException;

            _nativeFilter = NativeUnhandledExceptionFilter;
            SetUnhandledExceptionFilter(_nativeFilter);

            _initialized = true;
            LogBreadcrumb($"Crash logger initialized. BaseDirectory={AppContext.BaseDirectory}");
            LogBreadcrumb($"Crash log path: {_logPath}");
        }
    }

    public static void LogBreadcrumb(string message)
    {
        WriteEntry("INFO", message);
    }

    public static void LogException(string source, Exception exception)
    {
        var builder = new StringBuilder();
        builder.AppendLine(source);
        builder.AppendLine(exception.ToString());
        WriteEntry("ERROR", builder.ToString().TrimEnd());
    }

    private static void CurrentDomain_UnhandledException(object sender, System.UnhandledExceptionEventArgs e)
    {
        if (e.ExceptionObject is Exception exception)
        {
            LogException(
                $"AppDomain.CurrentDomain.UnhandledException (terminating={e.IsTerminating})",
                exception);
        }
        else
        {
            WriteEntry(
                "ERROR",
                $"AppDomain.CurrentDomain.UnhandledException (terminating={e.IsTerminating}) {e.ExceptionObject}");
        }
    }

    private static void TaskScheduler_UnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
    {
        LogException("TaskScheduler.UnobservedTaskException", e.Exception);
        e.SetObserved();
    }

    private static int NativeUnhandledExceptionFilter(IntPtr exceptionPointers)
    {
        try
        {
            WriteEntry("NATIVE", DescribeNativeCrash(exceptionPointers));
            TryWriteMiniDump(exceptionPointers);
        }
        catch (Exception ex)
        {
            LogException("NativeUnhandledExceptionFilter", ex);
        }

        return ExceptionContinueSearch;
    }

    private static string DescribeNativeCrash(IntPtr exceptionPointers)
    {
        if (exceptionPointers == IntPtr.Zero)
        {
            return "Unhandled native exception (no exception pointers).";
        }

        var pointers = Marshal.PtrToStructure<ExceptionPointers>(exceptionPointers);
        if (pointers.ExceptionRecord == IntPtr.Zero)
        {
            return "Unhandled native exception (no exception record).";
        }

        var record = Marshal.PtrToStructure<ExceptionRecord>(pointers.ExceptionRecord);
        return
            $"Unhandled native exception code=0x{record.ExceptionCode:X8} address=0x{record.ExceptionAddress.ToInt64():X16}";
    }

    private static void TryWriteMiniDump(IntPtr exceptionPointers)
    {
        var dumpPath = Path.Combine(
            _dumpDir,
            $"mtgacoach-{DateTime.Now:yyyyMMdd-HHmmssfff}.dmp");

        using var stream = new FileStream(dumpPath, FileMode.Create, FileAccess.ReadWrite, FileShare.None);
        var exInfo = new MiniDumpExceptionInformation
        {
            ThreadId = GetCurrentThreadId(),
            ExceptionPointers = exceptionPointers,
            ClientPointers = false,
        };

        var exInfoPtr = Marshal.AllocHGlobal(Marshal.SizeOf<MiniDumpExceptionInformation>());
        try
        {
            Marshal.StructureToPtr(exInfo, exInfoPtr, false);
            var ok = MiniDumpWriteDump(
                GetCurrentProcess(),
                GetCurrentProcessId(),
                stream.SafeFileHandle.DangerousGetHandle(),
                MiniDumpNormal,
                exInfoPtr,
                IntPtr.Zero,
                IntPtr.Zero);

            if (ok)
            {
                WriteEntry("INFO", $"Native crash dump written: {dumpPath}");
            }
            else
            {
                WriteEntry("WARN", $"MiniDumpWriteDump failed with Win32={Marshal.GetLastWin32Error()}");
            }
        }
        finally
        {
            Marshal.FreeHGlobal(exInfoPtr);
        }
    }

    private static void WriteEntry(string level, string message)
    {
        lock (_sync)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
            File.AppendAllText(
                _logPath,
                $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] [{level}] {message}{Environment.NewLine}{Environment.NewLine}");
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExceptionPointers
    {
        public IntPtr ExceptionRecord;
        public IntPtr ContextRecord;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExceptionRecord
    {
        public uint ExceptionCode;
        public uint ExceptionFlags;
        public IntPtr NestedExceptionRecord;
        public IntPtr ExceptionAddress;
        public uint NumberParameters;
        public uint Padding;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 15)]
        public nuint[] ExceptionInformation;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MiniDumpExceptionInformation
    {
        public int ThreadId;
        public IntPtr ExceptionPointers;
        [MarshalAs(UnmanagedType.Bool)]
        public bool ClientPointers;
    }

    private delegate int UnhandledExceptionFilterDelegate(IntPtr exceptionPointers);
}
