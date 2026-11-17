# 墨写 Markdown 桌面编辑器

![墨写图标](src/assets/moxie.png)

一个使用 Python/Tkinter 编写的 Windows Markdown 编辑器，包含独立桌面窗口、实时预览和 stdio MCP 服务。当前版本：**2.0.5**。

## 目录结构

```text
moxie-markdown/
├── src/                     程序源码（入口 desktop.pyw、mcp_server.py 等）
│   └── assets/              图标 moxie.png / moxie.ico
├── docs/                    中文说明文档（使用说明、MCP 接入、详细指南、示例）
├── tests/                   自动化测试脚本
├── tools/                   打包辅助脚本
├── build.ps1                一键构建 Windows 程序
├── moxie.spec               PyInstaller 打包配置
├── requirements.txt         运行依赖
├── requirements-build.txt   构建依赖
├── mcp-config.example.json  MCP 客户端配置示例
└── README.md                本文件
```

从源码运行时，程序会在本文件夹下自动创建 `data/`（草稿、历史版本等本机状态）和 `Documents/`（默认 MCP 文档目录）。它们已列入 `.gitignore`，不会进入源码仓库。

## 功能

- 新建、打开、保存、另存为、最近文档，识别常见中文编码。
- 编辑与预览双栏、专注编辑、阅读模式、字号调整。
- 点击大纲同时定位源码和预览，支持重复标题、下划线式标题及键盘选择。
- 顶部保留新建、打开、保存、查找四个按钮，语法插入统一使用编辑区鼠标右键菜单。
- H1–H6、粗体、斜体、删除线、引用、列表、任务清单、代码、链接、图片、表格、脚注等。
- 本地图片、带边框表格、任务清单和删除线预览。
- 查找替换、撤销重做、列表与任务回车续写。
- 自动恢复草稿、最近 30 次保存版本、外部文件修改检测。
- HTML 与中文 PDF 导出；PDF 保留粗体、斜体、行内代码、删除线、链接、嵌套列表和代码块。
- 浅色 / 深色主题（工具栏月亮按钮或“视图 → 深色模式”），编辑区字体可选等宽或中文清晰字体。
- 大纲高亮光标所在标题；Tab / Shift+Tab 批量缩进多行。
- MCP 接入说明、文档内指向本地 `.md` 的链接都在墨写内部打开。
- 11 个 MCP 工具：文件读写、精确替换、打开编辑器，以及读取/修改/保存当前编辑区。

详细操作见 [使用说明](docs/使用说明.md)，LLM 接入见 [MCP 接入说明](docs/MCP接入说明.md)。

## 内置使用指南

在程序中选择 **帮助 → 关于与详细用法**，可搜索 62 项功能，查看步骤并复制模板；也可直接查看 [详细使用指南](docs/详细使用指南.md)。指南覆盖全部菜单功能、Markdown 语法、代码块、完整文档模板以及 11 个 MCP 工具。

## 从源码运行

已在 Windows 11 x64、Python 3.14 上验证。Python 安装需包含 Tkinter。

在本文件夹（`moxie-markdown/`，不是 `src/`）打开 PowerShell，依次运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe src\desktop.pyw
```

三行命令分别是：创建虚拟环境 `.venv`、安装依赖、启动编辑器窗口。以后再次启动只需运行最后一行。

打开指定文档：

```powershell
.\.venv\Scripts\pythonw.exe src\desktop.pyw "D:\Notes\README.md"
```

## 构建独立 Windows 程序

```powershell
.\build.ps1
```

脚本创建本地虚拟环境、安装构建依赖（`requirements-build.txt`），用 PyInstaller 按 `moxie.spec` 打包，再运行 `tools/prepare_distribution.py` 整理发布目录。输出为 `dist/Moxie/`：

- `Moxie-2.0.5.exe`：桌面编辑器。
- `MoxieMCP-2.0.5.exe`：MCP 入口，需传入 `--mcp`。
- `_internal/`：共用运行库，必须与 EXE 一起保留。
- `mcp-config.json`：根据实际构建位置生成的接入配置。
- `Documents/`：默认 MCP 文档目录，内含 `示例.md`。
- `使用说明.md`、`MCP接入说明.md`、`详细使用指南.md`、`示例.md`：从 `docs/` 复制到 EXE 旁边的说明文档。
- `licenses/`：所用第三方库的许可证文件。

独立程序运行时无需系统 Python。源码仓库不包含构建产物、用户文档、恢复草稿或本机状态。

## MCP 接入

源码模式：

```powershell
.\.venv\Scripts\python.exe src\mcp_server.py --root "D:\Notes"
```

此命令等待 MCP 客户端通过标准输入/输出通信，不会显示交互式命令界面。

打包模式的客户端配置示例见 [mcp-config.example.json](mcp-config.example.json)。修改其中的绝对路径，或使用构建输出中的 `mcp-config.json`。`--root` 限定允许读写的 Markdown 目录。

LLM 应先读取文档，再使用返回的 `version` 作为修改请求的 `expected_version`。编辑区更新是可撤销的未保存修改，保存需要单独调用工具。

## 验证

安装依赖后，在 Windows 桌面会话中、于本文件夹根目录运行（测试脚本会自动找到 `src/` 中的源码）：

```powershell
.\.venv\Scripts\python.exe tests\test_outline.py
.\.venv\Scripts\python.exe tests\test_syntax.py
.\.venv\Scripts\python.exe tests\test_v2.py
.\.venv\Scripts\python.exe tests\test_guide.py
.\.venv\Scripts\python.exe tests\test_ui.py
```

覆盖大纲同步跳转、右键菜单、格式插入、图片和表格、PDF 导出、恢复与历史、真实 MCP stdio 调用，以及路径边界和版本冲突。测试会创建临时窗口与临时文件；不会修改个人文档。

## 文件结构

### `src/` 程序源码

- `desktop.pyw`：从源码启动桌面编辑器的入口。
- `launcher.py`：打包程序的共用入口；带 `--mcp` 参数时启动 MCP 服务，否则打开桌面窗口。
- `desktop_app.py` / `native_base.py`：桌面窗口与文档操作。
- `syntax_tools.py` / `writing_extension.py`：右键语法菜单与扩展语法。
- `rendering.py` / `outline.py`：阅读预览、PDF 导出及大纲定位。
- `core.py`：存储、备份、编码和路径边界；同时定义源码目录、程序根目录与 `docs/` 的位置。
- `theme.py`：浅色/深色配色、字体选择、工具栏图标。
- `viewer.py`：在墨写内部只读显示附带的说明文档。
- `help_content.py` / `usage_guide.py`：内置使用指南的内容与可搜索的“关于与详细用法”窗口。
- `mcp_server.py`：标准 MCP 工具。
- `assets/`：程序图标 `moxie.png`、`moxie.ico`。

### `docs/` 说明文档

- `使用说明.md`：日常操作说明。
- `MCP接入说明.md`：LLM 客户端接入方法。
- `详细使用指南.md`：内置使用指南的完整文字版。
- `示例.md`：体验用示例文档。

程序内“MCP 接入说明”等菜单直接读取这里的文件；打包时会复制到 EXE 旁边。

### `tests/` 测试

- `test_outline.py`、`test_syntax.py`、`test_v2.py`、`test_guide.py`、`test_ui.py`：见上文“验证”。

### `tools/` 辅助脚本

- `prepare_distribution.py`：打包后整理 `dist/Moxie/`，复制说明文档、生成 `mcp-config.json`、收集许可证。

### 根目录构建文件

- `build.ps1` / `moxie.spec`：Windows 打包。
- `requirements.txt` / `requirements-build.txt`：运行依赖与构建依赖。
- `mcp-config.example.json`：打包版 MCP 客户端配置示例。

## 当前范围

采用 Markdown 源码编辑与实时预览，尚不支持阅读区所见即所得编辑、LaTeX、Mermaid、自动下载网络图片、多人协作或 DOCX 导出。复杂 HTML/CSS 不保证与浏览器一致；PDF 使用简洁文档排版。
