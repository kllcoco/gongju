# Windows 右键径向菜单

这是阶段 1 的可运行版本，只负责托盘程序、`WH_MOUSE_LL` 低级鼠标钩子、短按/长按判定和原生右键补发。尚未实现转盘界面；收到阶段 1 实机验收确认后才进入阶段 2。

## 阶段 1 行为

- 右键按下先由钩子扣住，并记录物理屏幕坐标与鼠标下的目标窗口句柄。
- 在 200ms 内松开且移动距离小于 8 个物理像素时，通过 `SendInput` 补发右键按下和松开。
- 按住达到 200ms，或移动达到 8 个物理像素时，识别为长按并取消本次右键。阶段 1 会显示托盘提示，阶段 2 才显示转盘。
- 自注入事件带专属 `dwExtraInfo` 标记，钩子直接放行，避免递归拦截。
- 钩子运行在独立消息线程；回调只更新状态并无阻塞入队。`SendInput`、通知和日志均在回调外执行。
- 退出时主动卸载钩子。即使任务管理器强制结束进程，Windows 也会自动移除进程拥有的低级钩子。
- DPI 模式为 Per-Monitor DPI Aware V2。

## 编译和启动

需要 Windows 10/11 x64 与 .NET 8 SDK。在 PowerShell 中运行：

```powershell
cd RadialMenu
dotnet restore .\RadialMenu.sln
dotnet build .\RadialMenu.sln -c Debug
.\src\RadialMenu.App\bin\Debug\net8.0-windows\RadialMenu.exe
```

程序没有主窗口。启动成功后，通知区域会出现“径向菜单（阶段 1）”图标。右键托盘图标可以暂停监听或退出。

## 阶段 1 验收

1. 分别在桌面、资源管理器、浏览器网页和记事本中短按右键，每个位置至少 25 次，共 100 次。
2. 确认每次都出现应用原生右键菜单，没有重复菜单、漏弹、鼠标卡住或菜单一闪而过。
3. 按住右键超过 200ms，确认出现“阶段 1：已识别长按”托盘提示，松开后不弹原生菜单。
4. 按住右键后快速横向移动超过 8px，确认同样识别为长按。
5. 从托盘选择“暂停监听”，确认右键完全恢复系统直通；再选择“恢复监听”继续测试。
6. 从托盘选择“退出”，确认退出后右键正常。
7. 再次启动程序，用任务管理器“结束任务”，确认进程消失后右键仍正常。

日志位于 `%APPDATA%\RadialMenu\logs`。如果出现漏弹或卡顿，请保留日志，并记录发生问题的应用、是否跨屏、屏幕缩放比例以及大致按压时长。

## 自动测试

状态机测试不依赖 Windows，可以运行：

```powershell
dotnet test .\tests\RadialMenu.Core.Tests\RadialMenu.Core.Tests.csproj -c Release
```

## 目录结构

```text
RadialMenu/
├─ RadialMenu.sln                      解决方案文件，包含下面三个项目
├─ src/
│  ├─ RadialMenu.App/                  托盘程序本体（WPF + WinForms，net8.0-windows，x64，输出 RadialMenu.exe）
│  │  ├─ App.xaml / App.xaml.cs        程序入口：启用 DPI、创建钩子和托盘、处理暂停/退出与全局异常
│  │  ├─ MouseHookService.cs           在独立线程安装 WH_MOUSE_LL，扣住右键、补发短按、上报长按
│  │  ├─ TrayIconController.cs         托盘图标、“暂停监听/恢复监听”“退出”菜单和气泡提示
│  │  ├─ DpiAwareness.cs               启动时再次声明 Per-Monitor DPI Aware V2
│  │  ├─ LogService.cs                 按天写日志到 %APPDATA%\RadialMenu\logs，写失败不影响程序
│  │  ├─ app.manifest                  以普通权限运行，声明 DPI 感知与长路径支持
│  │  └─ RadialMenu.App.csproj
│  └─ RadialMenu.Core/                 与 Windows 无关的纯逻辑库（net8.0）
│     ├─ RightButtonGestureTracker.cs  短按/长按状态机：200ms、8 像素阈值，令牌防止旧定时器干扰
│     └─ RadialMenu.Core.csproj
└─ tests/
   └─ RadialMenu.Core.Tests/           xUnit 测试，只测试 Core 里的状态机
      ├─ RightButtonGestureTrackerTests.cs
      └─ RadialMenu.Core.Tests.csproj
```

`RadialMenu.Core` 与 `RadialMenu.App` 的分工：判定“短按还是长按”的规则都放在 `RadialMenu.Core`，因此可以在任何系统上用 `dotnet test` 验证；`RadialMenu.App` 只负责和 Windows 打交道（钩子、`SendInput`、托盘、日志），再调用 Core 的状态机做判断。编译产生的 `bin/`、`obj/` 已在 `.gitignore` 中忽略。
