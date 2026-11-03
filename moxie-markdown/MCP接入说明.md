# 墨写 2.0 · MCP 接入说明

这是标准 stdio MCP 服务，使用官方 Python MCP SDK。任何支持本地 stdio MCP 的 LLM 客户端均可接入。无需启动网络服务，也不需要 API 密钥。

## 接入

1. 打开客户端的 MCP 配置。
2. 将构建输出目录 `dist/Moxie/mcp-config.json` 中的 `moxie-markdown` 配置合并进现有的 `mcpServers`；不要覆盖其他服务。
3. 重启或重新加载客户端的 MCP 服务。
4. 让 LLM 调用 `workspace_info`，应返回当前允许访问的文档目录。

配置指向本机的独立 `MoxieMCP-2.0.2.exe`，参数包括 `--mcp` 与 `--root`。默认文档目录是程序旁的 `Documents` 文件夹。想编辑其他位置的 Markdown，请把 `--root` 后的路径改成那个目录的绝对路径。迁移整个程序文件夹后，也需要更新配置中的绝对路径。

## 工具

| 工具 | 作用 |
| --- | --- |
| workspace_info | 查看可访问目录和能力 |
| list_documents | 列出、按文件名筛选 Markdown |
| read_document | 读取文件及版本编号 |
| create_document | 创建新文件，拒绝覆盖同名文件 |
| write_document | 校验版本后写入文件，并保留旧版 |
| replace_text | 按精确文本和匹配次数修改文件 |
| open_in_editor | 在独立编辑窗口中打开文件 |
| list_editor_sessions | 列出正在运行的编辑窗口 |
| get_editor_document | 读取窗口内的内容，包括未保存修改 |
| update_editor_document | 更新编辑区，作为可撤销的未保存修改 |
| save_editor_document | 明确保存当前编辑区内容 |

## 示例指令

- “在墨写工作目录中新建 notes/计划.md，写一份项目计划，然后在编辑器中打开。”
- “读取墨写当前窗口里的文档，修改措辞，并把结果放回编辑区，先不要保存。”
- “确认当前文档版本后，保存墨写窗口里的修改。”

更新文件或编辑区之前，LLM 应先读取，并把返回的 `version` 原样作为 `expected_version` 传回。文件版本按磁盘字节计算；编辑区版本按当前 UTF-8 文本计算，两者不能混用。如果版本已变化，应重新读取，不能直接重试旧版本。

多个编辑窗口打开时，先调用 `list_editor_sessions`，再明确指定 `session_id`。窗口对应的已保存文件也必须在 `--root` 目录内；未命名窗口可以修改，但第一次保存需要用户在界面中选择文件位置。

文件修改会直接落盘并自动备份；编辑区修改不会自动覆盖原文件。编辑器没有未保存修改时，会在约两秒内载入外部文件修改；存在未保存修改时会保留编辑区并提示冲突。

服务仅支持 Markdown 扩展名，单文件最大 20 MB，不提供删除文件或执行命令工具。历史备份保留最近 30 次，位于程序旁 `data/history`。

## 排错

- 找不到服务：检查 `command` 的绝对路径，保留整个 `_internal` 目录。
- 找不到工具：确认参数含 `--mcp`，且客户端支持 stdio MCP。
- 找不到窗口：先启动新版 `Moxie-2.0.2.exe`，旧版窗口不提供 MCP 接口。
- 目录边界错误：调整客户端配置的 `--root`，使文档位于该目录内。
- 版本冲突：重新读取文件或窗口内容，再决定如何合并修改。
- 客户端需批准工具调用：在该客户端按其工具授权方式操作；本程序不代替客户端管理授权。

官方 SDK 文档：https://py.sdk.modelcontextprotocol.io/
