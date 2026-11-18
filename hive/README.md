# 蜂巢 Hive · 让 AI Agent 操作你自己的电脑

让**任何 AI Agent**（Claude Desktop / Claude Code / Cursor 等 MCP 客户端，OpenAI / Claude 工具调用，或你自己写的程序）直接操作**你自己的每一台电脑**：家里的 Windows 显卡机（含 WSL2）、Linux 服务器、Mac，以及租用的云 GPU（RunPod、AutoDL、Vast）。Agent 可以执行命令、读写文件、跑后台任务、下载模型、部署 vLLM / Ollama / llama.cpp / ComfyUI / Docker 服务并测试它们；部署好的模型还能通过**模型网关**给你的聊天软件使用。

纯 Python 标准库实现（Python 3.9+，Windows / Linux / macOS），无需任何第三方依赖（装了 `psutil` 会更准确，可选）。设计文档：[docs/DESIGN.md](docs/DESIGN.md)，安全说明：[docs/SECURITY.md](docs/SECURITY.md)。

> ⚠️ 这等同于远程执行代码。Hive 只用于你自己的、明确授权的电脑：每次初始化都要你输入 `yes` 同意；所有调用写入审计日志；默认只监听本机；`hive node pause` / `hive node panic` 随时急停。

## 组成

| 部件 | 作用 |
| --- | --- |
| 节点 Node（`hive node run`） | 装在每台被控电脑上。执行命令、后台任务、文件、进程、托管模型服务、显卡信息。直连（监听端口）或中继（主动连 Hub，适合没有公网端口的云机器） |
| 中枢 Hub（`hive hub run`，可选） | Agent 只连一个地址就能操作所有电脑；提供远程 MCP（`/mcp`）、审批、`hive.pyz` 分发和 OpenAI 兼容的模型网关 |
| 接入层 | MCP（stdio 和 Hub 的 Streamable HTTP）、HTTP API、OpenAI / Anthropic 工具格式导出、Python 客户端、命令行 |

## 安装

```sh
pip install ./hive            # 或 pip install -e ./hive；可选 pip install "./hive[full]"（psutil）
hive version
```

也可以用单文件 `hive.pyz`（`hive dist build` 生成，`python3 hive.pyz …` 运行）。

## 快速上手：一台电脑

```sh
hive init node                      # 写配置和令牌；问你输入 yes；中国大陆可加 --mirror cn（HF 镜像 + 清华 PyPI）
hive node run                       # 前台运行；或 hive service install node（登录后自动启动）
hive mcp-config --local             # 打印 Claude Desktop / Cursor 的 MCP 配置（--client claude-code 打印 claude mcp add 命令）
```

把打印出来的配置放进 MCP 客户端，重启即可。之后 Agent 会看到 `nodes_list`、`shell_run`、`fs_read`、`service_define` 等工具。令牌放在 `~/.hive/tokens/`，**不会**写进 Agent 的配置。

常用命令：

```sh
hive node status                    # 运行状态、任务、服务
hive sh self -- nvidia-smi          # 直接执行命令（退出码 = 远端退出码）
hive call fs.stat path=~            # 调用任意动作
hive logs self j_xxxx -f            # 跟踪任务日志
hive push self ./a.gguf ~/models/a.gguf     # 断点续传上传（pull 反向）
hive node secret set HF_TOKEN       # 在节点上保存密钥（从提示/标准输入读，Agent 只能按名字引用）
hive approvals list | approve ap_… [--for 30m]   # 审批（cautious 预设或 --cautious 令牌）
hive node pause | resume | panic    # 急停
hive doctor                         # 自检（网络、代理、权限、Docker、WSL…）
hive audit tail -n 50               # 审计日志
```

## 多台电脑：Hub

推荐：家里装 Tailscale，在一台常开的电脑上跑 Hub，监听 tailnet 地址（WireGuard 加密，不需要证书）：

```sh
hive init hub --tailscale [--funnel]          # --funnel：云 GPU 通过 Tailscale Funnel（真实证书）连回家
hive hub run                                   # 或 hive service install hub
hive hub add-node desk --direct http://desk.tail1234.ts.net:7701 --generate   # 直连节点（按提示在节点上加 hub 令牌）
hive hub add-node pod-a --relay                # 中继节点：打印加入码和云机器的一行命令
hive hub add-agent claude [--cautious]         # Agent 令牌（只显示一次）+ 在 Agent 电脑上要运行的命令
hive hub status                                # 各节点在线、版本、身份变化
```

在云 GPU 上（RunPod / AutoDL 等）运行 `add-node --relay` 打印的那几行即可，它会下载 `hive.pyz`、校验 sha256、`hive join --install`。加入码泄漏了就 `hive hub rotate-token pod-a`。云机器的模型和数据放在 `/workspace`（重启不丢）；只有 `autostart=true` 的服务会在机器重启后自动恢复。

Agent 电脑上：

```sh
hive init client --name home --url http://100.x.y.z:7700 --token-file ~/.hive/tokens/home.token --default
hive mcp-config --client claude-desktop        # 或 claude-code / cursor
```

远程 MCP（Claude Code 等支持 HTTP 的客户端）：`claude mcp add --transport http hive "https://<hub>/mcp?wait_budget=110" --header "Authorization: Bearer <token>"`（这是唯一会把令牌写进 Agent 配置的方式，`hive hub add-agent` 会提醒）。

### Windows 显卡机

Windows 节点默认用 PowerShell；vLLM / SGLang 这类只支持 Linux 的东西放在 **WSL2 里的第二个节点**（`hive init node --wsl`，端口 7702），Windows 节点用 `--wsl-keepalive <发行版>` 保持 WSL 不休眠。Ollama、llama.cpp、ComfyUI 可以直接在 Windows 上跑。详见设计文档 §20.7 和 Agent 可读的 `guide('windows')`。

## 模型网关：在聊天软件里用部署好的模型

```sh
hive gateway token add chatbox --models 'desk-wsl/qwen'
hive gateway url desk-wsl qwen                 # 打印 Base URL（tailnet / 本机 / Docker / 公网）和填写方法
```

在 Cherry Studio、Chatbox、Open WebUI 等软件里选择 “OpenAI 兼容”，填 Base URL 和这个令牌即可。直连节点支持流式输出；云机器（中继）一次性返回。

## 代理与镜像（中国大陆）

节点的所有出站连接（下载、模型拉取、中继）都可以走你的 HTTP 代理（Clash `http://127.0.0.1:7890` 之类）：`hive init node --proxy URL`，或在配置 `network.proxy` 设置；局域网、tailnet、本机永远直连。`--mirror cn` 使用 hf-mirror.com 和清华 PyPI。Docker 拉镜像需要在 Docker 守护进程里配置镜像源。

## English quick start

Hive lets AI agents (MCP clients, OpenAI/Anthropic tool loops, scripts) operate machines you own: run commands, edit files, background jobs, processes, model servers (vLLM, Ollama, llama.cpp, ComfyUI, Docker), and use deployed models through an OpenAI-compatible gateway. Stdlib-only Python 3.9+.

```sh
pip install ./hive
hive init node && hive node run                 # one machine
hive mcp-config --local                         # MCP snippet for Claude Desktop / Cursor (--client claude-code)

hive init hub --tailscale && hive hub run       # several machines
hive hub add-node pod-a --relay                 # prints the join code / one-liner for a NAT'd GPU pod
hive hub add-agent claude                       # an agent token for another machine
```

Python:

```python
import hive
with hive.connect("home") as c:                 # a client.json connection
    print(c.call("shell.run", {"command": "nvidia-smi", "timeout": 30}, node="desk-wsl")["stdout"])
    tk = c.toolkit(wait_budget=90)              # tk.anthropic_tools(), tk.openai_tools(), tk.call(name, args)
```

Examples: [examples/agent_anthropic.py](examples/agent_anthropic.py) (Claude, manual tool loop; it opts into the API's server-side refusal fallback — remove `betas`/`fallbacks` if you do not want it), [examples/agent_openai.py](examples/agent_openai.py) (any OpenAI-compatible model, stdlib only), [examples/python_client.py](examples/python_client.py), [examples/raw_http.sh](examples/raw_http.sh), [examples/mcp/](examples/mcp/).

Security model in short: every request needs a token; tokens have roles (`readonly`, `files`, `operator`) and node lists; each node enforces its own policy (roots, deny lists, exec allowlist, network categories); risky calls can require the owner's approval (`--cautious`); secrets stay on the node and are referenced by name; everything is audited. See [docs/SECURITY.md](docs/SECURITY.md).

## 目录结构

```text
hive/
├─ pyproject.toml     安装（pip install ./hive，命令 hive）
├─ docs/              设计文档、安全说明、协议
├─ hive/              Python 源码（纯标准库）
├─ tests/             自动测试（python -m unittest discover -s tests -t .）
├─ examples/          接入示例
└─ deploy/            systemd / launchd / Windows 任务 / RunPod 启动脚本模板
```
