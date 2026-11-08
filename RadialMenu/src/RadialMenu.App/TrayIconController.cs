using System.Drawing;
using System.Windows.Forms;

namespace RadialMenu.App;

internal sealed class TrayIconController : IDisposable
{
    private readonly NotifyIcon _notifyIcon;
    private readonly ToolStripMenuItem _pauseItem;
    private readonly Action<bool> _setPaused;
    private bool _disposed;

    public TrayIconController(Action<bool> setPaused, Action exit)
    {
        _setPaused = setPaused;
        _pauseItem = new ToolStripMenuItem("暂停监听");
        _pauseItem.Click += (_, _) => TogglePaused();

        var exitItem = new ToolStripMenuItem("退出");
        exitItem.Click += (_, _) => exit();

        var menu = new ContextMenuStrip();
        menu.Items.Add(_pauseItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(exitItem);

        _notifyIcon = new NotifyIcon
        {
            Text = "径向菜单（阶段 1）",
            Icon = SystemIcons.Application,
            ContextMenuStrip = menu,
            Visible = true
        };
    }

    public void ShowNotification(string title, string message, int timeoutMilliseconds)
    {
        if (_disposed)
        {
            return;
        }

        _notifyIcon.BalloonTipTitle = title;
        _notifyIcon.BalloonTipText = message;
        _notifyIcon.BalloonTipIcon = ToolTipIcon.Info;
        _notifyIcon.ShowBalloonTip(timeoutMilliseconds);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _notifyIcon.Visible = false;
        _notifyIcon.ContextMenuStrip?.Dispose();
        _notifyIcon.Dispose();
    }

    private void TogglePaused()
    {
        _pauseItem.Checked = !_pauseItem.Checked;
        _pauseItem.Text = _pauseItem.Checked ? "恢复监听" : "暂停监听";
        _setPaused(_pauseItem.Checked);
    }
}
