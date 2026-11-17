# gongju · 工具箱

这里收着几个独立的小工具。每个工具占一个文件夹，各自带 README，互不依赖。

| 文件夹 | 工具 | 一句话介绍 | 语言 / 平台 |
| --- | --- | --- | --- |
| [`hive/`](hive/) | 蜂巢 Hive | 让任何 AI Agent 控制你自己的多台电脑，在上面部署、运行系统和模型（设计中） | Python 3.9+ · Windows / Linux / macOS |
| [`moxie-markdown/`](moxie-markdown/) | 墨写 Markdown | Markdown 桌面编辑器，支持实时预览、导出 PDF，并提供 MCP 接入 | Python / Tkinter · Windows |
| [`RadialMenu/`](RadialMenu/) | 右键径向菜单 | 长按右键呼出转盘菜单（当前为阶段 1：鼠标钩子与长按/短按判定） | C# / .NET 8 · Windows |
| [`SnowLuma/`](SnowLuma/) | SnowLuma 归档 | v2.4.0 原始发行包归档，以及原生组件恢复工程 | C++ / Node · Windows x64 |

## 约定

- 每个工具的源码、测试、文档都只放在它自己的文件夹里。
- 每个工具的 README 讲清楚三件事：它是什么、怎么运行、目录里每部分是干什么的。
- 构建产物、虚拟环境、个人数据不进仓库，由各自的 `.gitignore` 排除。
