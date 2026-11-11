# SnowLuma 2.4.0 原生组件恢复工程

这是从用户提供的 Windows x64 发行包继续逆向得到的独立 C++ 工程，不是原作者源码，也不是已经验证可替换原版的完整 QQ Hook。

## 当前结果

| 组件 | 可从源码编译的产物 | 验证状态 |
| --- | --- | --- |
| 注入器的 7 个 Node 接口 | `snowluma-win32-x64.node` | Release 编译通过；4 组只读/参数对照测试通过；加载、卸载尚未运行验证 |
| Hook 内嵌的常驻分发模块 | `snowluma_windows_resident_dispatch.dll` | Release 编译通过；ABI 和并发测试程序被 Windows 应用控制阻止启动 |
| 通信帧、PE 检查、整数与 UID 快照解码 | `hook_core.lib`，以及头文件 | 1,000 组分片通信帧、7 个变长整数案例、UID/字段边界和真实 Hook TLS 解析回归通过 |
| 完整 QQ Hook | **尚未生成可替换的 DLL** | QQ 内部对象布局、消息钩子、回执及完整管道服务仍有缺口 |

关键限制：原版 Hook 的 TLS 目录含 **8 字节静态线程局部存储**。新注入器没有实现静态 TLS 分配，会主动拒绝这类手动映射，因此目前不能通过新注入器的手动映射接口加载原版 Hook。2026-11-11 已修复普通卸载误用这一限制的错误，但普通 LoadLibrary 路径仍未通过动态验证。不能把“生成了 .node”理解为已经兼容原版 Hook。详见 [本轮修复记录](CHANGES-2026-11-11.md)。

## 编译

需要 Windows x64、Visual Studio C++ Build Tools、Windows SDK 和 Visual Studio 的 CMake 组件，以及获准运行本地 PowerShell 脚本的开发环境。本次使用 MSVC 14.51.36231、Windows SDK 10.0.26100.0。项目自带 Node 24.22.1、匹配 SDK 和 node-addon-api，不依赖上一个恢复工程，也不需要 npm install。

在工程目录执行：

```powershell
.\build.cmd
```

输出位于 `build/Release`。已编译供检查的三个产物另外放在 `artifacts/`。重编译使用 `src/`，不会把 `reference/` 中的原版二进制链接进新产物。`hook_core.lib` 是基础静态库，不是完整 Hook DLL。

GitHub 的 `SnowLuma/native-recovery/` 提供可浏览源码；完整 ZIP 位于它的上一级目录。浏览版不重复提交 Node 工具链，首次执行构建会验证相邻 ZIP 的 SHA-256，再恢复其中的 `toolchain/`。直接下载并解压完整 ZIP 则无需这一步。

本机经当前命令环境调用 build.ps1 的编译已通过；单独测试 build.cmd 时，它调用的 Windows PowerShell 因脚本执行策略拒绝启动。因此不能声称本机双击入口已验证可用。脚本不修改执行策略；SDK 恢复逻辑已另外验证。

## 测试

```powershell
# 仅运行已验证的 Node 只读/参数对照测试
.\build.cmd readonly-test

# 全部测试：包含自制宿主、测试 DLL 的加载/卸载，以及常驻模块 ABI 测试
.\build.cmd test
```

完整测试只针对工程自带的测试进程和 DLL；进程发现测试会使用名为 QQ.exe 的测试宿主，不操作实际 QQ 客户端。当前机器的 Windows Code Integrity 3077 事件确认阻止 `resident_tests.exe` 和 `fixture_host.exe` 启动，因此完整测试没有通过，不应声称运行验证完成。工程未修改系统防护设置，也没有实施绕过。

测试日志见 `validation/`；机器可读结果见 `validation/status.json`。只读测试包含 20 组无效参数对照。协议测试主要检查分片重组、边界及重编码一致性，不等同于真实 QQ 会话的端到端兼容性验证。

## 目录

- `src/`、`include/`：可编译的恢复实现。
- `tests/`：本地测试宿主、测试 DLL 和对照测试。
- `integration/native.d.ts`：恢复的接口声明，补上原声明遗漏的 `prepare` 字段；供后续集成参考。
- `reference/`：原版注入器、Hook、提取的常驻 DLL，只用于逆向证据和测试比较。
- `evidence/`：Ghidra 识别出的 2,934 个函数的伪代码、调用索引和字符串引用。**伪代码不能直接编译，函数数量也不代表语义恢复率。**
- `tools/ExportRecovery.java`：导出静态证据的 Ghidra 脚本。
- `RECONSTRUCTION.md`：函数地址对应关系、实现差异和下一步缺口。

没有自动替换此前 SnowLuma 工程中的原版二进制。发布目标为 `kllcoco/gongju` 的 `SnowLuma/native-recovery/`，完整分发包为相邻的 `SnowLuma-v2.4.0-native-recovery.zip`。

## 来源与许可

输入来自 `SnowLuma-v2.4.0-win-x64.zip`，SHA-256：
`93096cb6d4cf576999094e9f7d1c9fb08e22f6b41c879182f015e639dc437613`。
此前恢复的公开前端来源为 `TomyJan/SnowLuma` 的 v2.4.0，提交 `e44e4ced17c5911b9aa9db449ba29f2d9e567d2a`。

保留了 `reference/LICENSE.SnowLuma`、Node 和 node-addon-api 的许可文件。原版参考二进制、伪代码、恢复实现和第三方依赖的来源不同；本工程不声称拥有原作者版权，也不为原版组件另行授权。
