# SnowLuma v2.4.0

这个文件夹存放两样东西：

1. **SnowLuma v2.4.0 的 Windows x64 原始发行压缩包**：用户提供的原样文件，未做任何修改。
2. **原生组件恢复工程**（2026-11-11）：从上面的发行包逆向得到的独立 C++ 工程，可以从源码编译出部分原生组件。

## 这是什么

- **原始发行包**是给“只想保存或使用原版”的人准备的。据文件提供者说明，这是最后的开放版本。
- **恢复工程**是给“想研究或重新编译原生组件”的人准备的。它不是原作者源码，而是一个**可编译的恢复候选工程**，还**不是经过验证的完整替代品**（具体限制见下文“当前状态与限制”）。

## 目录结构

```text
SnowLuma/
├── README.md                                   ← 本说明
├── release/                                    ← 所有压缩包都在这里
│   ├── SnowLuma-v2.4.0-win-x64.zip             ← 原始发行包（未修改）
│   ├── SnowLuma-v2.4.0-native-recovery.zip     ← 恢复工程的完整 ZIP
│   └── SnowLuma-v2.4.0-native-recovery.zip.sha256  ← 上面 ZIP 的校验值
└── native-recovery/                            ← 恢复工程的可浏览源码
    ├── README.zh-CN.md                         ← 恢复工程的详细中文说明
    ├── CHANGES-2026-11-11.md                   ← 本轮修复记录
    ├── build.cmd / build.ps1                   ← 构建入口
    ├── src/  include/                          ← 恢复实现
    ├── tests/  validation/                     ← 测试与测试日志
    ├── reference/  evidence/                   ← 原版参考二进制与静态证据
    └── tools/                                  ← 辅助脚本（含工具链恢复脚本）
```

## 文件一览

### 原始发行包

- 文件：[`release/SnowLuma-v2.4.0-win-x64.zip`](release/SnowLuma-v2.4.0-win-x64.zip)
- 大小：43,339,979 字节
- SHA-256：`93096cb6d4cf576999094e9f7d1c9fb08e22f6b41c879182f015e639dc437613`
- 归档日期：2026-11-10
- 版本说明：据文件提供者说明，这是最后的开放版本。

相关权利与许可条款以原项目及压缩包内文件为准。

### 原生组件恢复工程（2026-11-11）

- [浏览源码及详细说明](native-recovery/README.zh-CN.md)
- [下载完整工程 ZIP](release/SnowLuma-v2.4.0-native-recovery.zip)（52,982,537 字节）
- [ZIP 的 SHA-256](release/SnowLuma-v2.4.0-native-recovery.zip.sha256)：`52fd97a741815a0c91d62b587f2fb2b8fd80145ddb6afaa890ebf0852fca0879`
- [本轮修复记录](native-recovery/CHANGES-2026-11-11.md)

## 怎么用

### 一、只想要原版

直接下载 [`release/SnowLuma-v2.4.0-win-x64.zip`](release/SnowLuma-v2.4.0-win-x64.zip) 即可。下载后建议先核对 SHA-256，确认文件完整、没有被改动。在 Windows 上打开 PowerShell，进入文件所在目录后执行：

```powershell
Get-FileHash .\SnowLuma-v2.4.0-win-x64.zip -Algorithm SHA256
```

输出的 `Hash` 应与上面列出的 SHA-256 一致（PowerShell 显示为大写字母，大小写不影响比对）。

### 二、想编译恢复工程

有两种获取方式，任选其一：

- **方式 A：下载完整 ZIP（最省事）。** 完整 ZIP 包含 Node 工具链、已编译产物和全部静态证据，解压后即可使用，无需额外恢复工具链。
- **方式 B：直接使用本仓库的 `native-recovery/` 目录。** 这里是可浏览版本，省略了重复的工具链、已编译产物和逐函数伪代码。首次执行 `native-recovery/build.cmd` 时，会先校验同级 `release/` 目录中的完整 ZIP（即 `SnowLuma/release/SnowLuma-v2.4.0-native-recovery.zip` 及其 `.sha256` 文件），校验通过后再从中恢复 Node 工具链。所以请保持 `release/` 与 `native-recovery/` 在同一个 `SnowLuma/` 文件夹下，不要单独挪走。

编译前需要准备：Visual Studio C++ Build Tools、Windows SDK 和 CMake。

在 `native-recovery` 目录中打开 PowerShell，执行：

```powershell
.\build.cmd
```

更多细节（输出位置、测试命令、各目录用途等）请看 [native-recovery/README.zh-CN.md](native-recovery/README.zh-CN.md)。

> 小提示：恢复工程的详细说明中提到，单独测试 `build.cmd` 时，它调用的 Windows PowerShell 曾因本机脚本执行策略拒绝启动，因此双击入口在作者机器上未被验证可用。如果你遇到类似情况，请先阅读详细说明；脚本本身不会修改执行策略。

## 当前状态与限制

可从源码编译注入器 `.node`、Hook 内嵌的常驻分发 DLL，以及通信帧/消息快照基础库。已修复普通卸载误拒绝合法 TLS 的错误，补充 UID 与整数快照解析、完整路径匹配和异常清理。

1,000 组通信帧、变长整数、UID/字段边界、原版 Hook TLS 解析回归，以及 4 组只读 Node 对照测试通过。

需要特别注意：

- 完整 QQ Hook 尚未恢复；
- 手动映射仍不支持原版 Hook 的静态 TLS；
- 加载/卸载与常驻模块的运行验证受本机 Windows 应用控制策略阻止。

因此这是可编译的恢复候选工程，不是经过验证的完整替代品。

原版 DLL 只作为参考与测试输入，不链接进入恢复产物。
