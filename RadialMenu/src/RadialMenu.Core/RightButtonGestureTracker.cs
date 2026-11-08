using System.Diagnostics;

namespace RadialMenu.Core;

/// <summary>
/// 屏幕物理像素坐标。低级鼠标钩子在 Per-Monitor V2 模式下提供物理坐标。
/// </summary>
public readonly record struct ScreenPoint(int X, int Y);

/// <summary>
/// 一次右键手势开始时冻结的上下文，后续动作始终使用这里记录的目标窗口。
/// </summary>
public readonly record struct RightButtonGesture(
    long Token,
    ScreenPoint Origin,
    nint TargetWindow);

public enum GestureCompletion
{
    None,
    ShortClick,
    LongPress
}

public readonly record struct LongPressTransition(
    bool Started,
    RightButtonGesture Gesture)
{
    public static LongPressTransition None => default;
}

public readonly record struct GestureEndResult(
    GestureCompletion Completion,
    RightButtonGesture Gesture)
{
    public static GestureEndResult None => default;
}

/// <summary>
/// 线程安全的右键判定状态机。时间使用 Stopwatch 单调时钟，避免系统时间调整干扰 200ms 阈值。
/// </summary>
public sealed class RightButtonGestureTracker
{
    private enum TrackingState
    {
        Idle,
        Pressed,
        LongPress
    }

    private readonly object _sync = new();
    private readonly long _holdThresholdTicks;
    private readonly long _movementThresholdSquared;
    private TrackingState _state;
    private RightButtonGesture _gesture;
    private long _startedAt;
    private long _nextToken;

    public RightButtonGestureTracker(
        TimeSpan? holdThreshold = null,
        int movementThresholdPixels = 8)
    {
        var threshold = holdThreshold ?? TimeSpan.FromMilliseconds(200);
        if (threshold <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(holdThreshold));
        }

        if (movementThresholdPixels <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(movementThresholdPixels));
        }

        _holdThresholdTicks = (long)Math.Ceiling(threshold.TotalSeconds * Stopwatch.Frequency);
        _movementThresholdSquared = (long)movementThresholdPixels * movementThresholdPixels;
    }

    public bool IsTracking
    {
        get
        {
            lock (_sync)
            {
                return _state != TrackingState.Idle;
            }
        }
    }

    public bool TryBegin(
        ScreenPoint origin,
        nint targetWindow,
        long timestamp,
        out RightButtonGesture gesture)
    {
        lock (_sync)
        {
            if (_state != TrackingState.Idle)
            {
                gesture = _gesture;
                return false;
            }

            _gesture = new RightButtonGesture(++_nextToken, origin, targetWindow);
            _startedAt = timestamp;
            _state = TrackingState.Pressed;
            gesture = _gesture;
            return true;
        }
    }

    public LongPressTransition ObserveMove(ScreenPoint current)
    {
        lock (_sync)
        {
            if (_state != TrackingState.Pressed)
            {
                return LongPressTransition.None;
            }

            var deltaX = (long)current.X - _gesture.Origin.X;
            var deltaY = (long)current.Y - _gesture.Origin.Y;
            if ((deltaX * deltaX) + (deltaY * deltaY) < _movementThresholdSquared)
            {
                return LongPressTransition.None;
            }

            _state = TrackingState.LongPress;
            return new LongPressTransition(true, _gesture);
        }
    }

    public LongPressTransition ObserveHoldThreshold(long token, long timestamp)
    {
        lock (_sync)
        {
            if (_state != TrackingState.Pressed ||
                _gesture.Token != token ||
                timestamp - _startedAt < _holdThresholdTicks)
            {
                return LongPressTransition.None;
            }

            _state = TrackingState.LongPress;
            return new LongPressTransition(true, _gesture);
        }
    }

    public GestureEndResult End(long timestamp)
    {
        lock (_sync)
        {
            if (_state == TrackingState.Idle)
            {
                return GestureEndResult.None;
            }

            // 即使定时器线程调度稍晚，也要由单调时钟保证 200ms 之后绝不会误判为短按。
            var elapsedTicks = timestamp - _startedAt;
            var isLongPress = _state == TrackingState.LongPress || elapsedTicks >= _holdThresholdTicks;
            var result = new GestureEndResult(
                isLongPress ? GestureCompletion.LongPress : GestureCompletion.ShortClick,
                _gesture);

            ResetUnsafe();
            return result;
        }
    }

    public bool Cancel()
    {
        lock (_sync)
        {
            if (_state == TrackingState.Idle)
            {
                return false;
            }

            ResetUnsafe();
            return true;
        }
    }

    private void ResetUnsafe()
    {
        _state = TrackingState.Idle;
        _gesture = default;
        _startedAt = 0;
    }
}
