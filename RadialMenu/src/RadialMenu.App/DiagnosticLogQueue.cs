using System.Collections.Concurrent;
using System.Diagnostics;

namespace RadialMenu.App;

internal enum DiagnosticEventKind
{
    PhysicalRightDown,
    PhysicalRightUp,
    ReplayQueued,
    SendInputStarted,
    SendInputReturned,
    InjectedRightDown,
    InjectedRightUp,
    LongPressQueued,
    LongPressWorkerStarted,
    LongPressDispatcherStarted,
    SlowHookCallback
}

internal readonly record struct DiagnosticEvent(
    DiagnosticEventKind Kind,
    long Timestamp,
    long GestureToken = 0,
    long Value = 0,
    long DurationTicks = 0);

/// <summary>
/// 钩子诊断专用异步队列。生产者只执行无阻塞入队，字符串格式化与文件写入都在独立线程完成。
/// </summary>
internal sealed class DiagnosticLogQueue : IDisposable
{
    private readonly ConcurrentQueue<DiagnosticEvent> _events = new();
    private readonly AutoResetEvent _stopSignal = new(false);
    private readonly Thread _writerThread;
    private readonly long _clockOrigin = Stopwatch.GetTimestamp();
    private readonly DateTimeOffset _timeOrigin = DateTimeOffset.Now;
    private int _stopping;

    public DiagnosticLogQueue()
    {
        _writerThread = new Thread(WriterThreadMain)
        {
            IsBackground = true,
            Name = "RadialMenu.DiagnosticLog"
        };
        _writerThread.Start();
    }

    public void Enqueue(DiagnosticEvent diagnosticEvent)
    {
        if (Volatile.Read(ref _stopping) == 0)
        {
            _events.Enqueue(diagnosticEvent);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _stopping, 1) != 0)
        {
            return;
        }

        _stopSignal.Set();
        var writerStopped = _writerThread.Join(TimeSpan.FromSeconds(2));
        DrainQueue();
        if (writerStopped)
        {
            _stopSignal.Dispose();
        }
    }

    private void WriterThreadMain()
    {
        while (Volatile.Read(ref _stopping) == 0)
        {
            DrainQueue();
            _stopSignal.WaitOne(TimeSpan.FromMilliseconds(50));
        }

        DrainQueue();
    }

    private void DrainQueue()
    {
        while (_events.TryDequeue(out var diagnosticEvent))
        {
            LogService.Info(Format(diagnosticEvent));
        }
    }

    private string Format(DiagnosticEvent diagnosticEvent)
    {
        var eventTime = _timeOrigin + Stopwatch.GetElapsedTime(_clockOrigin, diagnosticEvent.Timestamp);
        var prefix = $"[诊断 {eventTime:HH:mm:ss.fffffff} token={diagnosticEvent.GestureToken}]";
        return diagnosticEvent.Kind switch
        {
            DiagnosticEventKind.PhysicalRightDown => $"{prefix} 收到物理右键按下。",
            DiagnosticEventKind.PhysicalRightUp => $"{prefix} 收到物理右键松开。",
            DiagnosticEventKind.ReplayQueued => $"{prefix} ReplayRightClick 命令已入队。",
            DiagnosticEventKind.SendInputStarted => $"{prefix} 工作线程开始调用 SendInput。",
            DiagnosticEventKind.SendInputReturned =>
                $"{prefix} SendInput 返回，返回值={diagnosticEvent.Value}。",
            DiagnosticEventKind.InjectedRightDown => $"{prefix} 钩子收到带标记的注入右键按下。",
            DiagnosticEventKind.InjectedRightUp => $"{prefix} 钩子收到带标记的注入右键松开。",
            DiagnosticEventKind.LongPressQueued => $"{prefix} LongPressStarted 命令已入队。",
            DiagnosticEventKind.LongPressWorkerStarted => $"{prefix} 工作线程开始处理 LongPressStarted。",
            DiagnosticEventKind.LongPressDispatcherStarted =>
                $"{prefix} Dispatcher 开始执行 ShowNotification。",
            DiagnosticEventKind.SlowHookCallback =>
                $"{prefix} 钩子回调耗时 {Stopwatch.GetElapsedTime(0, diagnosticEvent.DurationTicks).TotalMilliseconds:F3}ms，消息=0x{diagnosticEvent.Value:X}。",
            _ => $"{prefix} 未知诊断事件。"
        };
    }
}
