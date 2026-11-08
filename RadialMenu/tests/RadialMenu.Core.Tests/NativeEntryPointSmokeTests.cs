using System.Runtime.InteropServices;

namespace RadialMenu.Core.Tests;

/// <summary>
/// Windows 原生入口点冒烟测试。每次新增 LibraryImport 声明，都必须把入口点加入对应列表。
/// </summary>
public sealed class NativeEntryPointSmokeTests
{
    [WindowsFact]
    [Trait("平台", "Windows")]
    public void 所有声明的原生入口点都存在()
    {
        VerifyExports(
            "user32.dll",
            "SetWindowsHookExW",
            "UnhookWindowsHookEx",
            "CallNextHookEx",
            "SendInput",
            "WindowFromPoint",
            "PostThreadMessageW",
            "GetMessageW",
            "TranslateMessage",
            "DispatchMessageW",
            "PeekMessageW",
            "RegisterHotKey",
            "UnregisterHotKey",
            "SetProcessDpiAwarenessContext");

        VerifyExports(
            "kernel32.dll",
            "GetCurrentThreadId",
            "GetModuleHandleW");
    }

    private static void VerifyExports(string libraryName, params string[] entryPoints)
    {
        var library = NativeLibrary.Load(libraryName);
        try
        {
            foreach (var entryPoint in entryPoints)
            {
                var address = NativeLibrary.GetExport(library, entryPoint);
                Assert.NotEqual(nint.Zero, address);
            }
        }
        finally
        {
            NativeLibrary.Free(library);
        }
    }
}

internal sealed class WindowsFactAttribute : FactAttribute
{
    public WindowsFactAttribute()
    {
        if (!OperatingSystem.IsWindows())
        {
            Skip = "该冒烟测试必须在 Windows 上加载系统 DLL。";
        }
    }
}
