using System.Runtime.InteropServices;

namespace RadialMenu.App;

internal static partial class DpiAwareness
{
    // Manifest 是主配置；此调用用于在尚未创建任何窗口时再次明确进程 DPI 上下文。
    private static readonly nint PerMonitorAwareV2 = new(-4);

    public static void EnablePerMonitorV2()
    {
        if (!SetProcessDpiAwarenessContext(PerMonitorAwareV2))
        {
            // 如果 manifest 已经生效，Windows 会拒绝再次设置，这不属于错误。
            LogService.Info("DPI 上下文已由 manifest 或宿主进程设置。");
        }
    }

    [LibraryImport("user32.dll", EntryPoint = "SetProcessDpiAwarenessContext", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetProcessDpiAwarenessContext(nint value);
}
