# SnowLuma 原生组件恢复候选工程

完整说明见 [README.zh-CN.md](README.zh-CN.md)，当前修复见 [CHANGES-2026-11-11.md](CHANGES-2026-11-11.md)。

在 Windows x64 上运行 `build.cmd`。首次构建会从上一级目录的完整 ZIP 校验并恢复 Node SDK。

**完整 QQ Hook 尚未恢复，加载/卸载及常驻模块的动态验证尚未完成。** 原版 Hook 带静态 TLS，当前手动映射器仍不支持它。详见 [验证状态](validation/status.json)。
