using System.ComponentModel;
using System.Runtime.InteropServices;

namespace RadialMenu.App;

/// <summary>
/// 在独立消息线程上注册 Ctrl+Alt+Q，确保鼠标路径异常时仍可直接退出程序。
/// </summary>
internal sealed partial class GlobalHotkeyService : IDisposable
{
    // 应用定义的热键 ID 必须位于 0x0000–0xBFFF。
    private const int HotkeyId = 0x524D;
    private const uint WmHotkey = 0x0312;
    private const uint WmQuit = 0x0012;
    private const uint ModAlt = 0x0001;
    private const uint ModControl = 0x0002;
    private const uint ModNoRepeat = 0x4000;
    private const uint VirtualKeyQ = 0x51;

    private readonly Action _exit;
    private readonly ManualResetEventSlim _ready = new();
    private readonly Thread _thread;
    private Exception? _startupError;
    private uint _threadId;
    private int _started;
    private int _disposed;

    public GlobalHotkeyService(Action exit)
    {
        _exit = exit;
        _thread = new Thread(ThreadMain)
        {
            IsBackground = true,
            Name = "RadialMenu.EmergencyHotkey"
        };
    }

    public void Start()
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        if (Interlocked.Exchange(ref _started, 1) != 0)
        {
            return;
        }

        _thread.Start();
        if (!_ready.Wait(TimeSpan.FromSeconds(5)))
        {
            throw new TimeoutException("等待紧急退出热键注册超时。");
        }

        if (_startupError is not null)
        {
            throw new InvalidOperationException("紧急退出热键注册失败。", _startupError);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        var threadId = Volatile.Read(ref _threadId);
        if (threadId != 0)
        {
            PostThreadMessage(threadId, WmQuit, 0, 0);
        }

        if (_thread.IsAlive)
        {
            _thread.Join(TimeSpan.FromSeconds(2));
        }

        _ready.Dispose();
    }

    private void ThreadMain()
    {
        try
        {
            _threadId = GetCurrentThreadId();
            PeekMessage(out _, 0, 0, 0, 0);
            if (!RegisterHotKey(0, HotkeyId, ModControl | ModAlt | ModNoRepeat, VirtualKeyQ))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            _ready.Set();
            while (GetMessage(out var message, 0, 0, 0) > 0)
            {
                if (message.Value == WmHotkey && message.WParam == HotkeyId)
                {
                    _exit();
                }
            }
        }
        catch (Exception exception)
        {
            _startupError = exception;
            _ready.Set();
        }
        finally
        {
            if (_threadId != 0)
            {
                UnregisterHotKey(0, HotkeyId);
            }
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private readonly struct Point
    {
        public readonly int X;
        public readonly int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private readonly struct Message
    {
        public readonly nint Window;
        public readonly uint Value;
        public readonly nuint WParam;
        public readonly nint LParam;
        public readonly uint Time;
        public readonly Point Point;
        public readonly uint Private;
    }

    [LibraryImport("user32.dll", EntryPoint = "RegisterHotKey", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool RegisterHotKey(nint window, int id, uint modifiers, uint virtualKey);

    [LibraryImport("user32.dll", EntryPoint = "UnregisterHotKey", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool UnregisterHotKey(nint window, int id);

    [LibraryImport("user32.dll", EntryPoint = "PostThreadMessageW", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool PostThreadMessage(uint threadId, uint message, nuint wParam, nint lParam);

    [LibraryImport("user32.dll", EntryPoint = "GetMessageW", SetLastError = true)]
    private static partial int GetMessage(out Message message, nint window, uint minimum, uint maximum);

    [LibraryImport("user32.dll", EntryPoint = "PeekMessageW")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool PeekMessage(
        out Message message,
        nint window,
        uint minimum,
        uint maximum,
        uint removeMessage);

    [LibraryImport("kernel32.dll", EntryPoint = "GetCurrentThreadId")]
    private static partial uint GetCurrentThreadId();
}
