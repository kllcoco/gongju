# 灵枢 LingShu · Agent 控制中枢

> 状态：**设计阶段**。架构设计文档完成并审定后才开始写代码；本 README 会在实现完成后更新为完整使用说明。

## 这是什么

让**任何 AI Agent**（Claude Desktop / Claude Code / Cursor 等支持 MCP 的客户端，或者 OpenAI / Claude 工具调用、你自己写的程序）都能直接操作**你自己的每一台电脑**，例如家里的 Windows 显卡机、Linux 服务器、Mac，以及租用的云 GPU 机器（比如 RunPod）。目标是让 Agent 能在这些电脑上部署并运行系统和模型。

## 计划中的组成

| 部件 | 作用 |
| --- | --- |
| 节点 Node | 装在每台被控电脑上的小程序。负责执行命令、管理后台任务、读写文件、管理进程、托管模型服务（vLLM / Ollama / llama.cpp 等），也能查看显卡信息 |
| 中枢 Hub | 可选的总控台。Agent 只需连一个地址，就能操作所有电脑；没有公网端口的云机器会主动连上来领任务 |
| 接入层 | MCP 服务、HTTP 接口、OpenAI / Anthropic 工具格式导出、Python 客户端、命令行 |

## 安全原则

这套系统等同于远程执行代码，所以必须做到以下几点：口令认证；每个 Agent 只能访问授权给它的电脑；每台电脑有本地策略（只读模式、允许的目录、命令黑白名单）；操作全部写入审计日志；默认只监听本机。它只用于你自己配置、明确授权的电脑，不是隐蔽工具。

## 目录结构

```text
lingshu/
├─ README.md          本说明
├─ docs/              设计文档（DESIGN.md，设计完成后生成）
├─ lingshu/           Python 源码（纯标准库，Python 3.9+）
│  ├─ errors.py       统一错误类型
│  └─ util.py         口令、JSON、截断、编码等通用小工具
├─ tests/             自动测试
├─ examples/          接入示例
└─ deploy/            安装为系统服务的脚本
```
