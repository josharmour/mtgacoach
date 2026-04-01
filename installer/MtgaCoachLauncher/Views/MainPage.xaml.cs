using MtgaCoachLauncher.Models;

namespace MtgaCoachLauncher.Views;

public partial class MainPage : Page
{
    private RuntimeState? _state;
    private CoachProcess? _coachProcess;

    public MainPage()
    {
        this.InitializeComponent();
        this.Loaded += MainPage_Loaded;
    }

    private void MainPage_Loaded(object sender, RoutedEventArgs e)
    {
        _state = RuntimeDetector.DetectRuntimeState();

        // Auto-start coach and go straight to Coach tab
        NavView.SelectedItem = NavView.MenuItems[0];
        AutoStartCoach();
    }

    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is NavigationViewItem item)
        {
            var tag = item.Tag?.ToString();
            switch (tag)
            {
                case "coach":
                    ContentFrame.Navigate(typeof(CoachPage), this);
                    if (_coachProcess is not null && ContentFrame.Content is CoachPage cp)
                        cp.AttachProcess(_coachProcess);
                    break;
                case "repair":
                    ContentFrame.Navigate(typeof(RepairPage), this);
                    break;
            }
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
        NavView.SelectedItem = NavView.MenuItems[0]; // Coach tab
    }
}
