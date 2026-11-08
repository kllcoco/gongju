using System.Collections.Concurrent;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using RadialMenu.Core;

namespace RadialMenu.App;

internal enum GestureObservedKind
{
    LongPressStarted,
    LongPressReleased
}

internal sealed class GestureObservedEventArgs(
    GestureObservedKind kind,
    RightButtonGesture gesture) : EventArgs
{
    public GestureObservedKind Kind { get; } = kind;
    public RightButtonGesture Gesture { get; } = gesture;
}

/// <summary>
/// 在独立消息线程中安装 WH_MOUSE_LL。钩子回调只做常数时间状态更新和无阻塞入队。
/// </summary>
internal sealed partial class MouseHookService : IDisposable
{
    private const int WhMouseLl = 14;
    private const uint WmQuit = 0x0012;
    private const uint WmMouseMove = 0x0200;
    private const uint WmRButtonDown = 0x0204;
    private const uint WmRButtonUp = 0x0205;
    private const uint InputMouse = 0;
    private const uint MouseEventRightDown = 0x0008;
    private const uint MouseEventRightUp = 0x0010;
    private const int HoldMilliseconds = 200;

    // “RADIALMM”的 ASCII 数值。所有自注入事件都带此标记，钩子看到后立即放行。
    private static readonly nuint InjectionMarker = unchecked((nuint)0x52414449414C4D4DUL);

    private readonly RightButtonGestureTracker _tracker = new();
    private readonly BlockingCollection<WorkerCommand> _workerCommands = new();
    private readonly ManualResetEventSlim _hookReady = new();
    private readonly LowLevelMouseProc _hookCallback;
    private readonly Thread _hookThread;
    private readonly Thread _workerThread;
    private readonly System.Threading.Timer _holdTimer;
    private nint _hookHandle;
    private uint _hookThreadId;
    private Exception? _startupError;
    private int _enabled = 1;
    private int _started;
    private int _disposed;

    public MouseHookService()
    {
        _hookCallback = HookCallback;
        _holdTimer = new System.Threading.Timer(OnHoldTimer, null, Timeout.Infinite, Timeout.Infinite);
        _hookThread = new Thread(HookThreadMain)
        {
            IsBackground = true,
            Name = "RadialMenu.MouseHook"
        };
        _workerThread = new Thread(WorkerThreadMain)
        {
            IsBackground = true,
            Name = "RadialMenu.MouseWorker"
        };
    }

    public event EventHandler<GestureObservedEventArgs>? GestureObserved;
    public event EventHandler<string>? InjectionFailed;

    public bool Enabled
    {
        get => Volatile.Read(ref _enabled) != 0;
        set
        {
            Volatile.Write(ref _enabled, value ? 1 : 0);
            if (!value)
            {
                _holdTimer.Change(Timeout.Infinite, Timeout.Infinite);
                _tracker.Cancel();
            }
        }
    }

    public void Start()
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        if (Interlocked.Exchange(ref _started, 1) != 0)
        {
            return;
        }

        _workerThread.Start();
        _hookThread.Start();
        if (!_hookReady.Wait(TimeSpan.FromSeconds(5)))
        {
            throw new TimeoutException("等待低级鼠标钩子启动超时。");
        }

        if (_startupError is not null)
        {
            throw new InvalidOperationException("低级鼠标钩子安装失败。", _startupError);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        Enabled = false;
        _holdTimer.Dispose();

        var threadId = Volatile.Read(ref _hookThreadId);
        if (threadId != 0)
        {
            PostThreadMessage(threadId, WmQuit, 0, 0);
        }

        if (_hookThread.IsAlive && !_hookThread.Join(TimeSpan.FromSeconds(2)))
        {
            LogService.Error("钩子线程未在两秒内退出；Windows 会在进程结束时自动移除钩子。");
        }

        _workerCommands.CompleteAdding();
        if (_workerThread.IsAlive)
        {
            _workerThread.Join(TimeSpan.FromSeconds(2));
        }

        _workerCommands.Dispose();
        _hookReady.Dispose();
    }

    private void HookThreadMain()
    {
        try
        {
            _hookThreadId = GetCurrentThreadId();
            PeekMessage(out _, 0, 0, 0, 0);
            _hookHandle = SetWindowsHookEx(WhMouseLl, _hookCallback, GetModuleHandle(null), 0);
            if (_hookHandle == 0)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            _hookReady.Set();
            while (GetMessage(out var message, 0, 0, 0) > 0)
            {
                TranslateMessage(in message);
                DispatchMessage(in message);
            }
        }
        catch (Exception exception)
        {
            _startupError = exception;
            _hookReady.Set();
            LogService.Error("钩子线程异常。", exception);
        }
        finally
        {
            if (_hookHandle != 0)
            {
                UnhookWindowsHookEx(_hookHandle);
                _hookHandle = 0;
            }
        }
    }

    private nint HookCallback(int code, nuint message, nint data)
    {
        if (code != 0)
        {
            return CallNextHookEx(_hookHandle, code, message, data);
        }

        var mouse = Marshal.PtrToStructure<MsllHookStruct>(data);
        if (mouse.ExtraInfo == InjectionMarker || !Enabled)
        {
            return CallNextHookEx(_hookHandle, code, message, data);
        }

        switch ((uint)message)
        {
            case WmRButtonDown:
                if (_tracker.TryBegin(
                        new ScreenPoint(mouse.Point.X, mouse.Point.Y),
                        WindowFromPoint(mouse.Point),
                        Stopwatch.GetTimestamp(),
                        out var gesture))
                {
                    Interlocked.Exchange(ref _currentTimerToken, gesture.Token);
                    _holdTimer.Change(HoldMilliseconds, Timeout.Infinite);
                }

                // 同一次物理按下的重复消息也要扣住，避免系统看到不成对的右键事件。
                return 1;

            case WmMouseMove:
                var transition = _tracker.ObserveMove(new ScreenPoint(mouse.Point.X, mouse.Point.Y));
                if (transition.Started)
                {
                    _holdTimer.Change(Timeout.Infinite, Timeout.Infinite);
                    TryQueue(new WorkerCommand(WorkerCommandKind.LongPressStarted, transition.Gesture));
                }

                return CallNextHookEx(_hookHandle, code, message, data);

            case WmRButtonUp:
                if (!_tracker.IsTracking)
                {
                    return CallNextHookEx(_hookHandle, code, message, data);
                }

                _holdTimer.Change(Timeout.Infinite, Timeout.Infinite);
                var result = _tracker.End(Stopwatch.GetTimestamp());
                if (result.Completion == GestureCompletion.ShortClick)
                {
                    TryQueue(new WorkerCommand(WorkerCommandKind.ReplayRightClick, result.Gesture));
                }
                else if (result.Completion == GestureCompletion.LongPress)
                {
                    TryQueue(new WorkerCommand(WorkerCommandKind.LongPressReleased, result.Gesture));
                }

                return 1;

            default:
                return CallNextHookEx(_hookHandle, code, message, data);
        }
    }

    private void OnHoldTimer(object? state)
    {
        // 定时器只在当前手势仍处于 Pressed 时生效；这里取 token 防止旧定时回调污染新手势。
        // 由于 Timer.Change 后仍可能有已排队回调，先使用一个快照 token 由状态机二次校验。
        if (!_tracker.IsTracking)
        {
            return;
        }

        var transition = ObserveCurrentHoldThreshold();
        if (transition.Started)
        {
            TryQueue(new WorkerCommand(WorkerCommandKind.LongPressStarted, transition.Gesture));
        }
    }

    private LongPressTransition ObserveCurrentHoldThreshold()
    {
        // Timer 的 state 在 Begin 时不能无分配地更新，因此利用当前手势快照完成校验。
        // TryBegin 返回的 token 会写入字段，旧回调只有 token 相同时才能改变状态。
        var token = Interlocked.Read(ref _currentTimerToken);
        return _tracker.ObserveHoldThreshold(token, Stopwatch.GetTimestamp());
    }

    private long _currentTimerToken;

    private void TryQueue(WorkerCommand command)
    {
        if (Volatile.Read(ref _disposed) != 0)
        {
            return;
        }

        try
        {
            _workerCommands.TryAdd(command);
        }
        catch (InvalidOperationException)
        {
            // 退出期间 CompleteAdding 可能与定时器回调并发；此时丢弃通知即可。
        }
    }

    private void WorkerThreadMain()
    {
        foreach (var command in _workerCommands.GetConsumingEnumerable())
        {
            try
            {
                switch (command.Kind)
                {
                    case WorkerCommandKind.ReplayRightClick:
                        ReplayRightClick();
                        break;
                    case WorkerCommandKind.LongPressStarted:
                        GestureObserved?.Invoke(
                            this,
                            new GestureObservedEventArgs(GestureObservedKind.LongPressStarted, command.Gesture));
                        break;
                    case WorkerCommandKind.LongPressReleased:
                        GestureObserved?.Invoke(
                            this,
                            new GestureObservedEventArgs(GestureObservedKind.LongPressReleased, command.Gesture));
                        break;
                }
            }
            catch (Exception exception)
            {
                LogService.Error("鼠标工作线程执行失败。", exception);
            }
        }
    }

    private void ReplayRightClick()
    {
        var inputs = new[]
        {
            CreateMouseInput(MouseEventRightDown),
            CreateMouseInput(MouseEventRightUp)
        };

        var sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<Input>());
        if (sent != (uint)inputs.Length)
        {
            var error = new Win32Exception(Marshal.GetLastWin32Error());
            InjectionFailed?.Invoke(
                this,
                $"原生右键补发不完整（{sent}/{inputs.Length}）：{error.Message}");
        }
    }

    private static Input CreateMouseInput(uint flags) => new()
    {
        Type = InputMouse,
        Union = new InputUnion
        {
            Mouse = new MouseInput
            {
                Flags = flags,
                ExtraInfo = InjectionMarker
            }
        }
    };

    private enum WorkerCommandKind
    {
        ReplayRightClick,
        LongPressStarted,
        LongPressReleased
    }

    private readonly record struct WorkerCommand(
        WorkerCommandKind Kind,
        RightButtonGesture Gesture);

    [StructLayout(LayoutKind.Sequential)]
    private readonly struct Point(int x, int y)
    {
        public readonly int X = x;
        public readonly int Y = y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private readonly struct MsllHookStruct
    {
        public readonly Point Point;
        public readonly uint MouseData;
        public readonly uint Flags;
        public readonly uint Time;
        public readonly nuint ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Input
    {
        public uint Type;
        public InputUnion Union;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)]
        public MouseInput Mouse;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MouseInput
    {
        public int X;
        public int Y;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public nuint ExtraInfo;
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

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate nint LowLevelMouseProc(int code, nuint message, nint data);

    [LibraryImport("user32.dll", EntryPoint = "SetWindowsHookExW", SetLastError = true)]
    private static partial nint SetWindowsHookEx(
        int hookId,
        LowLevelMouseProc callback,
        nint module,
        uint threadId);

    [LibraryImport("user32.dll", EntryPoint = "PostThreadMessageW", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool UnhookWindowsHookEx(nint hook);

    [LibraryImport("user32.dll")]
    private static partial nint CallNextHookEx(nint hook, int code, nuint message, nint data);

    [LibraryImport("user32.dll", EntryPoint = "GetMessageW", SetLastError = true)]
    private static partial uint SendInput(uint count, [In] Input[] inputs, int size);

    [LibraryImport("user32.dll", EntryPoint = "DispatchMessageW")]
    private static partial nint WindowFromPoint(Point point);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool PostThreadMessage(uint threadId, uint message, nuint wParam, nint lParam);

    [LibraryImport("user32.dll", SetLastError = true)]
    private static partial int GetMessage(out Message message, nint window, uint minimum, uint maximum);

    [LibraryImport("user32.dll", EntryPoint = "PeekMessageW")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool TranslateMessage(in Message message);

    [LibraryImport("user32.dll")]
    private static partial nint DispatchMessage(in Message message);

    [LibraryImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool PeekMessage(
        out Message message,
        nint window,
        uint minimum,
        uint maximum,
        uint removeMessage);

    [LibraryImport("kernel32.dll")]
    private static partial uint GetCurrentThreadId();

    [LibraryImport("kernel32.dll", EntryPoint = "GetModuleHandleW", StringMarshalling = StringMarshalling.Utf16)]
    private static partial nint GetModuleHandle(string? moduleName);
}
