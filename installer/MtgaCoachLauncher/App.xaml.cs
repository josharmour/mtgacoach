using System.Runtime.InteropServices;

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
        this.UnhandledException += App_UnhandledException;

        // Allocate a hidden console so child processes (Python) inherit it.
        // PortAudio needs a console for audio device enumeration.
        AllocConsole();
        var consoleWnd = GetConsoleWindow();
        if (consoleWnd != IntPtr.Zero)
            ShowWindow(consoleWnd, 0); // SW_HIDE
    }

    private void App_UnhandledException(object sender, Microsoft.UI.Xaml.UnhandledExceptionEventArgs e)
    {
        var crashLog = System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "mtgacoach", "crash.log");
        try
        {
            System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(crashLog)!);
            System.IO.File.AppendAllText(crashLog,
                $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {e.Exception}\n\n");
        }
        catch { }
        e.Handled = true;
    }

    protected override void OnLaunched(LaunchActivatedEventArgs e)
    {
        _window = new Window
        {
            Title = $"mtgacoach v{RuntimeDetector.ReadVersion()}",
        };

        _window.Content = new MainPage();

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_window);
        var windowId = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = Microsoft.UI.Windowing.AppWindow.GetFromWindowId(windowId);
        appWindow.Resize(new Windows.Graphics.SizeInt32(1000, 780));

        _window.Activate();
    }
}
