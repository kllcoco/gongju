# SnowLuma v2.4.0

保存用户提供的 Windows x64 原始发行压缩包，文件未修改。

- 文件：`SnowLuma-v2.4.0-win-x64.zip`
- 大小：43,339,979 字节
- SHA-256：`93096cb6d4cf576999094e9f7d1c9fb08e22f6b41c879182f015e639dc437613`
- 归档日期：2026-11-10
- 版本说明：据文件提供者说明，这是最后的开放版本。

[下载压缩包](./SnowLuma-v2.4.0-win-x64.zip)

相关权利与许可条款以原项目及压缩包内文件为准。

## 原生组件恢复工程（2026-11-11）

- [浏览源码及详细说明](native-recovery/README.zh-CN.md)
- [下载完整工程 ZIP](SnowLuma-v2.4.0-native-recovery.zip)
- [ZIP 的 SHA-256](SnowLuma-v2.4.0-native-recovery.zip.sha256)
- [本轮修复记录](native-recovery/CHANGES-2026-11-11.md)

可从源码编译注入器 `.node`、Hook 内嵌的常驻分发 DLL，以及通信帧/消息快照基础库。已修复普通卸载误拒绝合法 TLS 的错误，补充 UID 与整数快照解析、完整路径匹配和异常清理。

1,000 组通信帧、变长整数、UID/字段边界、原版 Hook TLS 解析回归，以及 4 组只读 Node 对照测试通过。完整 QQ Hook 尚未恢复；手动映射仍不支持原版 Hook 的静态 TLS；加载/卸载与常驻模块的运行验证受本机 Windows 应用控制策略阻止。因此这是可编译的恢复候选工程，不是经过验证的完整替代品。

完整 ZIP 包含 Node 工具链、已编译产物和全部静态证据。仓库可浏览目录省略重复工具链、已编译产物和逐函数伪代码；首次执行 `native-recovery/build.cmd` 会校验相邻 ZIP 并恢复 Node 工具链。需要 Visual Studio C++ Build Tools、Windows SDK 和 CMake。原版 DLL 只作为参考与测试输入，不链接进入恢复产物。
