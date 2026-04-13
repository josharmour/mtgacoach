using System.Runtime.InteropServices;
using MtgaCoachLauncher.Services;

namespace MtgaCoachLauncher;

public partial class App : Application
{
    private Window? _window;
    public Window? MainWindow => _window;

    [DllImport("kernel32.dll")]
    private static extern bool AllocConsole();

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    public App()
    {
        this.InitializeComponent();
        CrashLogger.Initialize();
        this.UnhandledException += App_UnhandledException;

        // Allocate a hidden console so child processes (Python) inherit it.
        // PortAudio needs a console for audio device enumeration.
        AllocConsole();
        var consoleWnd = GetConsoleWindow();
        if (consoleWnd != IntPtr.Zero)
            ShowWindow(consoleWnd, 0); // SW_HIDE

        CrashLogger.LogBreadcrumb("App initialized.");
    }

    private void App_UnhandledException(object sender, Microsoft.UI.Xaml.UnhandledExceptionEventArgs e)
    {
        CrashLogger.LogException("App.UnhandledException", e.Exception);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs e)
    {
        CrashLogger.LogBreadcrumb("OnLaunched starting.");
        _window = new Window
        {
            Title = $"mtgacoach v{RuntimeDetector.ReadVersion()}",
        };

        _window.Content = new MainPage();
        _window.Activated += (_, args) =>
            CrashLogger.LogBreadcrumb($"Window activated: {args.WindowActivationState}");

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_window);
        var windowId = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = Microsoft.UI.Windowing.AppWindow.GetFromWindowId(windowId);
        appWindow.Resize(new Windows.Graphics.SizeInt32(1000, 780));

        _window.Activate();
        CrashLogger.LogBreadcrumb(
            $"Main window activated. Title={_window.Title}; AppRoot={RuntimeDetector.GetAppRoot()}");
    }
}
