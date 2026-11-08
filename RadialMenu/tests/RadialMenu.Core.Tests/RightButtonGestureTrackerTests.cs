using System.Diagnostics;
using RadialMenu.Core;

namespace RadialMenu.Core.Tests;

public sealed class RightButtonGestureTrackerTests
{
    private static readonly long Millisecond = Stopwatch.Frequency / 1000;

    [Fact]
    public void 小于二百毫秒且移动不足八像素判定为短按()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(100, 100), (nint)42, 1_000, out _);

        var transition = tracker.ObserveMove(new ScreenPoint(107, 100));
        var result = tracker.End(1_000 + (199 * Millisecond));

        Assert.False(transition.Started);
        Assert.Equal(GestureCompletion.ShortClick, result.Completion);
        Assert.Equal((nint)42, result.Gesture.TargetWindow);
    }

    [Fact]
    public void 移动达到八像素立即判定为长按()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(100, 100), (nint)7, 1_000, out _);

        var transition = tracker.ObserveMove(new ScreenPoint(108, 100));
        var result = tracker.End(1_000 + (20 * Millisecond));

        Assert.True(transition.Started);
        Assert.Equal(GestureCompletion.LongPress, result.Completion);
    }

    [Fact]
    public void 对角线移动按欧氏距离判断()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(100, 100), (nint)7, 1_000, out _);

        var transition = tracker.ObserveMove(new ScreenPoint(106, 106));

        Assert.True(transition.Started);
    }

    [Fact]
    public void 松开时已超过二百毫秒即使定时器延迟也判定为长按()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(10, 20), (nint)99, 5_000, out _);

        var result = tracker.End(5_000 + (201 * Millisecond));

        Assert.Equal(GestureCompletion.LongPress, result.Completion);
    }

    [Fact]
    public void 恰好二百毫秒属于长按()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(10, 20), (nint)99, 5_000, out _);

        var result = tracker.End(5_000 + (200 * Millisecond));

        Assert.Equal(GestureCompletion.LongPress, result.Completion);
    }

    [Fact]
    public void 旧定时器令牌不能影响下一次手势()
    {
        var tracker = new RightButtonGestureTracker();
        tracker.TryBegin(new ScreenPoint(0, 0), (nint)1, 1_000, out var first);
        tracker.End(1_000 + (10 * Millisecond));
        tracker.TryBegin(new ScreenPoint(0, 0), (nint)2, 2_000, out _);

        var transition = tracker.ObserveHoldThreshold(first.Token, 2_000 + (10 * Millisecond));
        var result = tracker.End(2_000 + (10 * Millisecond));

        Assert.False(transition.Started);
        Assert.Equal(GestureCompletion.ShortClick, result.Completion);
    }

    [Fact]
    public void 无活动手势时松开不会产生动作()
    {
        var tracker = new RightButtonGestureTracker();

        var result = tracker.End(Stopwatch.GetTimestamp());

        Assert.Equal(GestureCompletion.None, result.Completion);
    }
}
