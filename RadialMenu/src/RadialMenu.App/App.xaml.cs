using System.Windows;
using System.Windows.Threading;

namespace RadialMenu.App;

public partial class App : System.Windows.Application
{
    private MouseHookService? _mouseHook;
    private TrayIconController? _trayIcon;
    private int _isStopping;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        RegisterGlobalExceptionHandlers();
        DpiAwareness.EnablePerMonitorV2();

        try
        {
            _mouseHook = new MouseHookService();
            _trayIcon = new TrayIconController(
                setPaused: SetPaused,
                exit: StopApplication);
            _mouseHook.GestureObserved += OnGestureObserved;
            _mouseHook.InjectionFailed += OnInjectionFailed;
            _mouseHook.Start();
            _trayIcon.ShowNotification("径向菜单", "阶段 1 已运行：短按右键保持原生行为。", 1800);
            LogService.Info("程序启动，低级鼠标钩子安装成功。");
        }
        catch (Exception exception)
        {
            LogService.Error("启动失败。", exception);
            System.Windows.MessageBox.Show(
                $"径向菜单启动失败：\n{exception.Message}\n\n详情已写入日志。",
                "径向菜单",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            StopApplication();
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        DisposeRuntimeResources();
        base.OnExit(e);
    }

    private void SetPaused(bool paused)
    {
        if (_mouseHook is null)
        {
            return;
        }

        _mouseHook.Enabled = !paused;
        _trayIcon?.ShowNotification(
            "径向菜单",
            paused ? "已暂停，右键事件完全放行。" : "已恢复右键手势监听。",
            1200);
        LogService.Info(paused ? "用户暂停监听。" : "用户恢复监听。");
    }

    private void OnGestureObserved(object? sender, GestureObservedEventArgs e)
    {
        if (e.Kind != GestureObservedKind.LongPressStarted)
        {
            return;
        }

        Dispatcher.BeginInvoke(() =>
            _trayIcon?.ShowNotification(
                "阶段 1：已识别长按",
                "本次右键已取消；转盘将在阶段 2 接入。",
                1000));
    }

    private void OnInjectionFailed(object? sender, string error)
    {
        Dispatcher.BeginInvoke(() =>
        {
            LogService.Error(error);
            _trayIcon?.ShowNotification("右键补发失败", error, 3000);
        });
    }

    private void StopApplication()
    {
        if (Interlocked.Exchange(ref _isStopping, 1) != 0)
        {
            return;
        }

        Dispatcher.BeginInvoke(() =>
        {
            DisposeRuntimeResources();
            Shutdown();
        });
    }

    private void DisposeRuntimeResources()
    {
        if (_mouseHook is not null)
        {
            _mouseHook.GestureObserved -= OnGestureObserved;
            _mouseHook.InjectionFailed -= OnInjectionFailed;
            _mouseHook.Dispose();
            _mouseHook = null;
        }

        _trayIcon?.Dispose();
        _trayIcon = null;
        LogService.Info("程序退出，低级鼠标钩子已卸载。");
    }

    private void RegisterGlobalExceptionHandlers()
    {
        DispatcherUnhandledException += (_, args) =>
        {
            LogService.Error("UI 线程发生未处理异常。", args.Exception);
            args.Handled = true;
            _trayIcon?.ShowNotification("径向菜单发生错误", "详情已写入日志。", 3000);
            StopApplication();
        };

        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            LogService.Error("进程发生未处理异常。", args.ExceptionObject as Exception);

        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            LogService.Error("后台任务发生未观察异常。", args.Exception);
            args.SetObserved();
        };
    }
}
