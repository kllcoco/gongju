using System.Text;
using System.IO;

namespace RadialMenu.App;

internal static class LogService
{
    private static readonly object Sync = new();
    private static readonly string LogDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "RadialMenu",
        "logs");

    public static void Info(string message) => Write("信息", message, null);

    public static void Error(string message, Exception? exception = null) =>
        Write("错误", message, exception);

    private static void Write(string level, string message, Exception? exception)
    {
        try
        {
            lock (Sync)
            {
                Directory.CreateDirectory(LogDirectory);
                var path = Path.Combine(LogDirectory, $"{DateTime.Now:yyyyMMdd}.log");
                var line = new StringBuilder()
                    .Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff"))
                    .Append(" [").Append(level).Append("] ")
                    .AppendLine(message);

                if (exception is not null)
                {
                    line.AppendLine(exception.ToString());
                }

                File.AppendAllText(path, line.ToString(), Encoding.UTF8);
            }
        }
        catch
        {
            // 日志失败绝不能反过来影响鼠标钩子或程序退出。
        }
    }
}
