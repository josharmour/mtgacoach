using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml.Navigation;
using MtgaCoachLauncher.Services;

namespace MtgaCoachLauncher.Views;

public partial class MainPage : Page
{
    private RuntimeState? _state;
    private CoachProcess? _coachProcess;
    private readonly DispatcherQueue _dispatcher;

    public MainPage()
    {
        this.InitializeComponent();
        _dispatcher = DispatcherQueue.GetForCurrentThread();
        this.Loaded += MainPage_Loaded;
        ContentFrame.Navigated += ContentFrame_Navigated;
    }

    private async void MainPage_Loaded(object sender, RoutedEventArgs e)
    {
        _state = RuntimeDetector.DetectRuntimeState();
        CrashLogger.LogBreadcrumb(
            $"MainPage loaded. AppRoot={RuntimeDetector.GetAppRoot()} Python={_state?.PythonExe ?? "missing"} Source={_state?.PythonSource ?? "unknown"}");

        // Delay navigation slightly to let XAML type system finish initialization.
        // This avoids a race in CoreMessagingXP.dll during page activation.
        await Task.Delay(50);

        // Auto-start coach and go straight to Coach tab
        NavView.SelectedItem = NavView.MenuItems[0];
        AutoStartCoach();
    }

    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is NavigationViewItem item)
        {
            var tag = item.Tag?.ToString();
            if ((tag == "coach" && ContentFrame.Content is CoachPage) ||
                (tag == "repair" && ContentFrame.Content is RepairPage))
                return;

            // Defer navigation to a fresh dispatcher tick so XAML activation
            // doesn't collide with the SelectionChanged callback stack.
            _dispatcher.TryEnqueue(DispatcherQueuePriority.Normal, () =>
            {
                try
                {
                    if (ContentFrame.Content is CoachPage currentCoachPage)
                        currentCoachPage.DetachProcess();

                    switch (tag)
                    {
                        case "coach":
                            CrashLogger.LogBreadcrumb("Navigating to CoachPage");
                            ContentFrame.Navigate(typeof(CoachPage));
                            break;
                        case "repair":
                            CrashLogger.LogBreadcrumb("Navigating to RepairPage");
                            ContentFrame.Navigate(typeof(RepairPage));
                            break;
                    }
                }
                catch (Exception ex)
                {
                    CrashLogger.LogException("MainPage.NavView_SelectionChanged", ex);
                    System.Diagnostics.Debug.WriteLine($"Navigation failed: {ex.Message}");
                    SummaryText.Text = $"Coach page failed to load: {ex.Message}";
                }
            });
        }
    }

    private void ContentFrame_Navigated(object sender, NavigationEventArgs e)
    {
        switch (ContentFrame.Content)
        {
            case CoachPage coachPage:
                coachPage.AttachMainPage(this);
                if (_coachProcess is not null)
                    coachPage.AttachProcess(_coachProcess);
                break;
            case RepairPage repairPage:
                repairPage.AttachMainPage(this);
                if (_state is not null)
                    repairPage.UpdateState(_state);
                break;
        }
    }

    public RuntimeState? State => _state;
    public CoachProcess? CoachProcess => _coachProcess;

    public void RefreshState()
    {
        _state = RuntimeDetector.DetectRuntimeState();
        if (ContentFrame.Content is RepairPage repairPage)
            repairPage.UpdateState(_state);
    }

    public void NavigateToRepair()
    {
        NavView.SelectedItem = NavView.MenuItems[1];
    }

    private async void AutoStartCoach()
    {
        if (_state?.PythonExe is null)
        {
            SummaryText.Text = "Python not found. Go to Repair to set up.";
            NavView.SelectedItem = NavView.MenuItems[1]; // Repair
            return;
        }

        _coachProcess = new CoachProcess();
        try
        {
            _coachProcess.Start(autopilot: false, dryRun: false, afk: false);
            SummaryText.Text = "Coach is running.";

            if (ContentFrame.Content is CoachPage cp)
                cp.AttachProcess(_coachProcess);
        }
        catch (Exception ex)
        {
            CrashLogger.LogException("MainPage.AutoStartCoach", ex);
            SummaryText.Text = $"Coach failed to start: {ex.Message}";
            var dialog = new ContentDialog
            {
                Title = "Coach Launch Failed",
                Content = $"{ex.Message}\n\n{_coachProcess.LastError}",
                CloseButtonText = "OK",
                XamlRoot = this.XamlRoot,
            };
            await dialog.ShowAsync();
            NavView.SelectedItem = NavView.MenuItems[1]; // Repair
        }
    }

    /// <summary>Restart the coach process (called from Repair tab).</summary>
    public void RestartCoach(bool autopilot, bool dryRun, bool afk)
    {
        _coachProcess?.Stop();
        _coachProcess?.Dispose();
        _coachProcess = new CoachProcess();
        _coachProcess.Start(autopilot, dryRun, afk);
        SummaryText.Text = "Coach is running.";
        if (ContentFrame.Content is CoachPage coachPage)
            coachPage.AttachProcess(_coachProcess);
        NavView.SelectedItem = NavView.MenuItems[0]; // Coach tab
    }
}
