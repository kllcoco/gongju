# 蜂巢 Hive · 让 AI 助手操作你自己的电脑

**一句话**：装上 Hive 以后，Claude Desktop、Claude Code、Cursor 这类 AI 助手（下面统称 **Agent**）就能在你**自己的**每一台电脑上执行命令、读写文件、跑后台任务、下载模型、部署 vLLM / Ollama / llama.cpp / ComfyUI / Docker 服务并测试它们；部署好的模型还能通过**模型网关**给 Cherry Studio、Open WebUI 这类聊天软件直接使用。

- 纯 Python 标准库写成（Python 3.9 或更新），Windows / Linux / macOS / WSL2 / 云 GPU（RunPod、AutoDL、Vast）都能用，不需要装任何第三方依赖（装了 `psutil` 进程信息会更准，可选）。
- 开发者参考：[docs/DESIGN.md](docs/DESIGN.md)（完整设计，所有细节以它为准）、[docs/SECURITY.md](docs/SECURITY.md)（安全说明与加固）、[docs/PROTOCOL.md](docs/PROTOCOL.md)（HTTP 协议）。

> ⚠️ **先读这一段**：Hive 等于“允许别人在你的电脑上远程执行代码”。只把它装在**你自己的、你明确同意被控制的**电脑上。每次初始化都要你亲手输入 `yes`；启动时会打印醒目的提示；所有操作都写进审计日志；默认只听本机 127.0.0.1；随时可以用 `hive node pause`（暂停）和 `hive node panic`（紧急停止）刹车。

---

## 目录

1. [它由哪几部分组成](#1-它由哪几部分组成)
2. [安装](#2-安装)（Windows / Linux / macOS / WSL2 / RunPod / 单文件 hive.pyz）
3. [先在一台电脑上试一试](#3-先在一台电脑上试一试)
4. [推荐搭法：家里一个中枢 + Tailscale（一步一步）](#4-推荐搭法家里一个中枢--tailscale一步一步)
5. [把 AI 助手接上来（MCP）](#5-把-ai-助手接上来mcp)
6. [模型网关：在聊天软件里用你部署的模型](#6-模型网关在聊天软件里用你部署的模型)
7. [日常命令](#7-日常命令)
8. [安全：谨慎口令、审批、暂停和紧急停止](#8-安全谨慎口令审批暂停和紧急停止)
9. [国内网络：代理与镜像](#9-国内网络代理与镜像)
10. [出问题了怎么办（hive doctor）](#10-出问题了怎么办hive-doctor)
11. [升级与卸载](#11-升级与卸载)
12. [目录结构](#12-目录结构)
13. [English quick start](#13-english-quick-start)

---

## 1. 它由哪几部分组成

| 部件 | 命令 | 作用 |
| --- | --- | --- |
| **节点 Node** | `hive node run` | 装在**每一台**要被 Agent 操作的电脑上。负责执行命令、后台任务、读写文件、管理进程、托管模型服务（崩了自动重启、可开机自启、健康检查）、查显卡、下载整个模型仓库。两种连法：**直连**（节点监听一个端口，等中枢连进来）和**中继**（节点自己主动连到中枢“领任务”，给没有公网端口的云 GPU 用）。 |
| **中枢 Hub** | `hive hub run` | 可选，但多台电脑时强烈推荐。Agent 只需要连这一个地址，就能操作所有电脑；云机器也是主动连到它。一般放在家里一台常开的电脑上，通过 Tailscale 访问。它还负责审批、给云机器分发 `hive.pyz`、以及模型网关。 |
| **接入层** | `hive mcp`、`hive mcp-config` 等 | Agent 连进来的方式：MCP（本机 stdio；中枢上还有远程 HTTP 版 `/mcp`）、HTTP JSON 接口、OpenAI / Anthropic 工具格式导出、Python 客户端、命令行。 |
| **模型网关** | 中枢和节点自带 | 把部署好的模型变成一个“OpenAI 兼容地址”，聊天软件、编程插件、你自己的脚本都能直接用，不经过 Agent。 |

几个名词：

- **口令（token）**：一串 `hv_` 开头的密码。每个 Agent、中枢、聊天软件各用各的口令，口令有“角色”（只读 / 文件 / 全部），可以随时吊销。口令文件放在 `~/.hive/tokens/`，**不会**写进 Agent 的配置文件。
- **Tailscale**：一个免费的“虚拟局域网”软件。你的每台电脑装上并登录同一个账号后，它们之间就像在一个局域网里，互相访问自动加密（WireGuard），不需要公网 IP、端口映射或证书。Hive 默认推荐用它。
- **`~`**：你的用户主目录。Windows 上是 `C:\Users\你的用户名`，Linux 是 `/home/你的用户名`，macOS 是 `/Users/你的用户名`。

---

## 2. 安装

所有平台都需要 **Python 3.9 或更新**。在终端里检查：

```sh
python3 --version        # Windows 上用：py --version
```

先把本仓库下载到电脑上：在 GitHub 页面点 **Code → Download ZIP** 解压，或者：

```sh
git clone https://github.com/kllcoco/gongju
cd gongju                # 下面的 ./hive 指的就是这个仓库里的 hive 文件夹
```

### 2.1 Windows

1. 安装 Python：到 <https://www.python.org/downloads/> 下载安装包，安装时**勾选 “Add python.exe to PATH”**（或者在 PowerShell 里 `winget install Python.Python.3.12`）。
2. 打开 **PowerShell**（不需要管理员），进入仓库文件夹，安装：

   ```powershell
   py -m pip install .\hive
   hive --version
   ```

   如果提示找不到 `hive`，关掉 PowerShell 重新打开一次（PATH 刚更新）。
3. 显卡机想跑 vLLM / SGLang？这些只能在 Linux 上跑，请再看 [2.4 WSL2](#24-wsl2windows-显卡机跑-vllm--sglang)。Ollama、llama.cpp、ComfyUI 可以直接在 Windows 上跑。

> Windows 上的节点以“你登录后自动启动”的方式运行（任务计划程序），**不需要**管理员权限，也不要用管理员身份运行 Hive。

### 2.2 Linux（Ubuntu / Debian 等）

新版 Ubuntu 不允许直接往系统 Python 里 `pip install`，所以装进一个独立的虚拟环境：

```sh
sudo apt install -y python3-venv          # 一般已经装好了
python3 -m venv ~/.venvs/hive
~/.venvs/hive/bin/pip install ./hive
mkdir -p ~/.local/bin && ln -sf ~/.venvs/hive/bin/hive ~/.local/bin/hive   # 以后直接敲 hive
hive --version                            # 找不到就重新打开终端
```

> 不要用 root 运行 Hive：节点会拒绝启动（`hive init node` 一开始就会提醒你）。原因是 Agent 能做的事等于运行 Hive 的那个用户能做的事，root 等于把整台电脑交出去。只有在“root 是唯一用户”的云 GPU 容器里才例外：`hive join` / `hive init node --pod` 会自动在配置里写 `"allow_root": true`，见 [2.5](#25-runpod--autodl--vast云-gpu)。
>
> `pip install ./hive` 会在 hive 文件夹里留下 `build/` 和 `hive_agent.egg-info/` 两个文件夹，这是正常的安装副产品（已写进 `.gitignore`），可以不管，也可以删掉。

### 2.3 macOS

到 <https://www.python.org/downloads/> 装 Python（或 `brew install python`），然后和 Linux 一样：

```sh
python3 -m venv ~/.venvs/hive
~/.venvs/hive/bin/pip install ./hive
mkdir -p ~/.local/bin && ln -sf ~/.venvs/hive/bin/hive ~/.local/bin/hive
hive --version
```

（若 `~/.local/bin` 不在 PATH 里，在 `~/.zshrc` 加一行 `export PATH="$HOME/.local/bin:$PATH"`。）

### 2.4 WSL2（Windows 显卡机跑 vLLM / SGLang）

思路：在 Windows 里的 WSL2 再装**第二个节点**（名字默认是 `<Windows 电脑名>-wsl`，下面用 `desk-wsl` 举例），把它当成一台普通的 Linux 电脑；Windows 上的节点负责在你登录后把 WSL 拉起来并保持运行。

1. **Windows（管理员 PowerShell）**：`wsl --install -d Ubuntu-24.04`，再 `wsl --update`。保持 Windows 上的 NVIDIA 驱动是新的；**千万不要在 WSL 里面装 NVIDIA 的 Linux 驱动**（WSL 自带）。
2. **可选**：在 `C:\Users\你\.wslconfig` 里写：

   ```ini
   [wsl2]
   memory=48GB                 # 约为内存的 75%（默认只给 50%）
   swap=16GB
   networkingMode=mirrored     # Windows 11 22H2 及以上推荐：WSL 和 Windows 共用网络
   ```

   然后 `wsl --shutdown`。
3. **在 WSL 里**：确认 `/etc/wsl.conf` 有

   ```ini
   [boot]
   systemd=true
   ```

   （新版 Ubuntu 默认就有），改过的话在 Windows 里 `wsl --shutdown` 再重新打开。
4. **在 WSL 里安装**（仓库在 Windows 的 D 盘时，路径类似 `/mnt/d/gongju/hive`）：

   ```sh
   python3 -m venv ~/.venvs/hive
   ~/.venvs/hive/bin/pip install /mnt/d/gongju/hive
   ~/.venvs/hive/bin/hive init node --wsl            # 国内加 --mirror cn；代理见第 9 节
   ~/.venvs/hive/bin/hive service install node       # systemd 用户服务；按提示执行 sudo loginctl enable-linger $USER
   ```

   在 WSL 里虚拟环境没有激活，直接敲 `hive` 会提示找不到，所以上面都写全路径；嫌麻烦就执行一次 `mkdir -p ~/.local/bin && ln -sf ~/.venvs/hive/bin/hive ~/.local/bin/hive`，再开一个新终端。
5. **回到 Windows**：让 Windows 节点保持 WSL 运行（Windows 节点已经初始化过也没关系，这条命令只加这一项设置）：

   ```powershell
   hive init node --wsl-keepalive Ubuntu-24.04
   ```

6. 怎么接到中枢，见 [4.3](#43-把家里其他电脑接进中枢) 末尾的表格。

注意：模型权重放在 WSL 自己的磁盘里（`~/models`），不要放 `/mnt/c`（非常慢）；WSL 里的 `nvidia-smi` 看不到每个进程用了多少显存，这是正常的。

### 2.5 RunPod / AutoDL / Vast（云 GPU）

云机器上**不用手动安装**：在中枢上执行 `hive hub add-node pod-a --relay`，它会打印几行命令，贴到云机器的网页终端里运行即可（自动下载 `hive.pyz`、校验 sha256、加入中枢、自动后台运行）。详见 [4.4](#44-云-gpurunpod-等)。

### 2.6 单文件 `hive.pyz`（免安装）

`hive.pyz` 是把整个 Hive 打成的一个文件，任何装了 Python 3.9+ 的电脑都能直接运行，不用 pip：

```sh
hive dist build --out hive.pyz      # 在已经装好 Hive 的电脑上生成（或在仓库里：cd hive && python3 -m hive dist build --out hive.pyz）
python3 hive.pyz --version          # 用法和 hive 命令完全一样：python3 hive.pyz init node …
```

中枢运行时也会在 `/v1/dist/hive.pyz` 提供这个文件，云机器就是从这里下载的。

---

## 3. 先在一台电脑上试一试

不需要中枢、不需要 Tailscale，五分钟就能让本机的 Claude Desktop 操作这台电脑：

```sh
hive init node          # 生成配置和口令；会问你是否同意，输入 yes
hive node run           # 前台运行（这个窗口别关）；之后可改成开机自启：hive service install node
```

在脚本里或不是终端的地方运行（没法输入 yes）时，加 `--yes` 表示你已经同意：`hive init node --yes`。同样，`hive node panic`、`hive approvals approve ap_xxxx` / `hive approvals deny ap_xxxx` 也会先问 y/N，不在终端里时加 `--yes`。

命令的输出是英文的，下面是 `hive init node` 和 `hive node run` 的典型输出，右边是中文解释：

```text
node 'desk' configured at /home/me/.hive/node/config.json (preset standard)   ← 节点名 desk（默认取电脑名），配置文件位置，策略档位
listen: http://127.0.0.1:7701                                                 ← 只在本机 7701 端口监听
tokens: …/tokens/this-pc.token (agent), …/tokens/this-pc-owner.token (owner)  ← 两个口令文件：给 Agent 用的、给你自己命令行用的
next: hive node run   (foreground)   or   hive service install node            ← 下一步：前台运行，或装成开机自启

Hive node 'desk' ACTIVE — remote agents may control this computer as user 'me' (preset: standard).   ← 节点已启动，Agent 能以 me 的身份操作这台电脑
Listen: http://127.0.0.1:7701 (plaintext, 2 tokens)  Relay: off  Paused: no  Audit: …/node/audit      ← 监听地址、口令个数、没开中继、没暂停、审计日志目录
restarting to apply new settings…                                            ← 你改了配置（例如加了口令），前台节点正在自动重启
```

前台运行（`hive node run` / `hive hub run` 开在终端窗口里）时，**改配置的命令会让它在同一个窗口里自动重启**并打印 `restarting to apply new settings…`，你什么都不用做；改配置的命令会打印 `applied (restarted …)`。节点没在运行时则打印 `will apply on next start`（下次启动生效）；节点由看门狗（云机器，或 `hive service install node` 装的 watchdog）管着、正好在重启时，打印 `applied by the watchdog`，表示几秒后看门狗用新设置把它拉起来。

再开一个终端：

```sh
hive node status                                   # 看看节点是否在运行
hive sh self -- echo 你好                           # 让本机执行一条命令（self 指本机节点）
hive mcp-config --client claude-desktop --write    # 把 Hive 写进 Claude Desktop 的配置
```

**完全退出** Claude Desktop（托盘图标右键退出）再打开，就能在工具列表里看到 `nodes_list`、`shell_run`、`fs_read`、`job_start`、`service_define` 等工具。试着对它说：“用 Hive 看看这台电脑的显卡和磁盘空间”。

`hive init node` 常用参数：

| 参数 | 作用 |
| --- | --- |
| `--preset cautious` | 谨慎档：执行命令、启动服务、删除、结束进程都要你批准（Agent 会浏览网页、读陌生文件时推荐）。另有 `readonly`（只读）、`standard`（默认）、`full`（不设目录限制） |
| `--root D:\models` / `--root ~/data:ro` | 额外允许 Agent 访问的目录；`:ro` 表示只读。默认只允许 `~` |
| `--mirror cn` | 国内：Hugging Face 镜像 + 清华 PyPI |
| `--proxy http://127.0.0.1:7890` | 指定 HTTP 代理（默认会自动探测） |
| `--tailscale` | 监听本机的 Tailscale 地址（给别的电脑上的中枢直连用） |

已经初始化过的节点，再执行 `hive init node` 加某个参数（例如 `hive init node --mirror cn`）只会修改这一项设置，口令不变；想全部重来用 `--force`（会生成新口令）。中枢也一样：`hive init hub --listen 127.0.0.1:7700`、`--tailscale`、`--public-url`、`--funnel`、`--with-local-node` 只改那一项。

---

## 4. 推荐搭法：家里一个中枢 + Tailscale（一步一步）

最终效果：

```text
 Claude Desktop / Claude Code / Cursor          Cherry Studio / Open WebUI
              │  (MCP，口令在本机文件里)                 │  (OpenAI 兼容地址 + 专用口令)
              └───────────────┬───────────────────────┘
                     中枢 Hub（家里常开的电脑，监听 Tailscale 地址 :7700）
          ┌──────────────┬───────────────┼──────────────────┐
     本机节点        家里其他电脑       desk-wsl（WSL2）     云 GPU pod-a
   127.0.0.1:7701   Tailscale :7701    :7702 直连或中继     主动连回中枢（中继）
```

### 4.0 准备：每台电脑装 Tailscale

到 <https://tailscale.com/download> 下载安装（Windows / macOS 是图形界面安装包；Linux 执行 `curl -fsSL https://tailscale.com/install.sh | sh` 再 `sudo tailscale up`），**所有电脑登录同一个账号**。装好后 `tailscale ip -4` 能看到一个 `100.x.y.z` 的地址，这就是这台电脑的 Tailscale IP。

然后在每台电脑上按[第 2 节](#2-安装)装好 Hive。下面的顺序照着做即可。

### 4.1 家里其他电脑（被控的电脑）

在每一台上：

```sh
hive init node --tailscale            # 监听本机 Tailscale 地址，中枢才能直连；国内加 --mirror cn
hive service install node             # 开机/登录后自动启动（Windows 是“登录后自动启动”）
```

已经初始化过的电脑，再执行 `hive init node --tailscale` 只改监听方式，口令不变。Windows 防火墙可能弹窗，或者用 `hive doctor` 看它打印的放行命令（只放行 Tailscale 网段 `100.64.0.0/10`）。

### 4.2 中枢（家里常开的那台电脑）

```sh
hive init node                                    # 本机节点，只听 127.0.0.1 就行（做过第 3 节的可跳过，再执行也无妨）
hive init hub --tailscale --with-local-node       # 中枢监听 Tailscale 地址，并自动把本机节点接进来
hive service install node
hive service install hub
```

- 要接 RunPod 这类**不在你 Tailscale 里**的云机器时，改成 `hive init hub --tailscale --with-local-node --funnel`，并按它的提示执行一次 `tailscale funnel --bg 7709`。Funnel 会给中枢一个带正规证书的公网地址 `https://<电脑名>.<你的 tailnet>.ts.net`（第一次用 Funnel 时 Tailscale 会给你一个网址，去后台点开启）。
- `--with-local-node` 会提醒你：在中枢这台电脑上能执行命令的 Agent 等于拿到了中枢。把这台电脑上 Agent 用的口令改成谨慎口令：`hive node token set this-pc --cautious`（口令本身不变，Agent 的配置不用改；见第 8 节）。
- `hive hub status` 查看各节点是否在线。刚加进来的节点会先显示 `checking`（中枢正在第一次联系它），几秒后变成 `online`；节点那边还没加中枢口令时显示 `offline`，备注栏（NOTE）写着 `hub token not accepted`，提醒你去那台电脑上运行刚才打印的 `hive node token add hub-…`；加好后几秒内自动变成 `online`。
- 本机节点已经在运行（比如第 3 节开着的 `hive node run`）也没关系：`--with-local-node` 会把中枢的口令加进本机节点的配置，并**自动重启本机节点**让它生效，打印 `applied (restarted the node)`；节点没在运行就打印 `will apply on next start`。
- **为什么本机节点在中枢里叫 `hubhost`**：节点和中枢默认都用电脑名当名字（比如都叫 `vm`）。为了在中枢的节点列表里不和中枢自己重名，本机节点在中枢里登记为 `hubhost`；如果你给节点起了别的名字（`hive init node --name desk`），中枢里就用 `desk`。`hive init hub` 会打印它用的名字，以后在中枢上操作本机就用这个名字，例如 `hive sh hubhost --connection hub-local -- nvidia-smi`。
- 用了 `--tailscale` 但这台电脑上 Tailscale 还没装好或没登录时，`hive init hub` 会立刻提醒（不会失败），装好并 `tailscale up` 后再 `hive hub run`。

### 4.3 把家里其他电脑接进中枢

在**中枢**上（把 IP 换成那台电脑 `tailscale ip -4` 的结果）：

```sh
hive hub add-node desk --direct http://100.101.1.5:7701 --generate
```

它会打印一行命令，例如 `hive node token add hub-home --kind hub --sha256 sha256:6be4…`，到 **desk 那台电脑**上运行它（只有口令的“指纹”在电脑之间传递，口令本身不会）。

**Windows 显卡机 WSL 里的节点**（`desk-wsl`，见 [2.4](#24-wsl2windows-显卡机跑-vllm--sglang)）有三种接法，`hive init node --wsl` 也会提示你选：

| 情况 | 做法 | 聊天软件能否“边生成边显示” |
| --- | --- | --- |
| 中枢就装在这台 Windows 电脑上 | 中枢上：`hive hub add-node desk-wsl --direct http://127.0.0.1:7702 --generate`，再在 WSL 里运行它打印的 `hive node token add …` | 能 |
| 中枢在别的电脑，WSL 用 mirrored 网络 | WSL 里：`~/.venvs/hive/bin/hive init node --tailscale`；中枢上：`hive hub add-node desk-wsl --direct http://100.101.1.5:7702 --generate`（IP 换成这台 Windows 的 Tailscale IP），再在 WSL 里运行打印的命令 | 能 |
| 中枢在别的电脑，WSL 是默认 NAT 网络 | 中枢上：`hive hub add-node desk-wsl --relay`，把打印的 `HIVE_JOIN='hvj1.…' ~/.venvs/hive/bin/hive join` 那行贴到 WSL 里运行 | 不能（整段生成完一次显示，照常可用） |

### 4.4 云 GPU（RunPod 等）

在**中枢**上：

```sh
hive hub add-node pod-a --relay --via public     # 需要 4.2 的 --funnel
```

它会打印一段命令（含一个只显示一次的“加入码” `hvj1.…`），大致是：

```sh
export HIVE_JOIN='hvj1.…'
mkdir -p /workspace/.hive && cd /workspace/.hive
curl -fsSL https://<中枢>.ts.net/v1/dist/hive.pyz -o hive.pyz
echo "<sha256>  hive.pyz" | sha256sum -c - || exit 1
HIVE_HOME=/workspace/.hive python3 hive.pyz join --install
```

把**它实际打印的内容**整段贴进 RunPod 的网页终端运行就行。`join --install` 最后会等最多 30 秒，确认节点真的启动并连上了中枢才打印 `joined: …`；如果节点起不来（例如同一台电脑上已经有别的 Hive 节点占用了端口 `Address already in use`），它会直接告诉你原因和修法（中继节点不需要监听端口：`hive init node --no-listen`）。之后：

- 在云机器的新终端里用 `/workspace/.hive/hive` 代替 `hive`，例如 `/workspace/.hive/hive node secret set HF_TOKEN`。
- 模型和数据放 `/workspace`（pod 重启不丢）；pod 重启后只有设了 `autostart=true` 的模型服务会自动起来。
- 做成 RunPod 模板：模板环境变量里放 `HIVE_JOIN='hvj1.…'`，启动命令用仓库里的 [deploy/runpod/start-hive.sh](deploy/runpod/start-hive.sh)（`add-node --relay` 也会打印模板写法）。新开的 pod 会自己从中枢下载并校验 Hive。
- 加入码泄露了（截图、聊天记录、分享的模板）：`hive hub rotate-token pod-a`，旧码立刻作废并打印新的。
- 不加 `--via` 时：中枢监听 Tailscale 就默认给出走 Tailscale 内网的加入码（给家里 NAT 模式的 WSL 这类机器用）；如果还开了 Funnel，会同时打印给云机器用的公网命令块。

### 4.5 停止与重启、云机器“换了机器”

- `hive node stop` / `hive hub stop` 会真正停下来（不会被系统服务马上拉起）；`hive node restart` / `hive hub restart` 才是重启。
- 中枢提示某台云机器“换了一台机器注册”（identity changed）：如果**不是你**换的，执行 `hive hub rotate-token pod-a`；确实是你新开的 pod，才执行 `hive hub ack-node pod-a`（会先显示对方的 IP、主机名让你确认）。

### 4.6 （可选）在节点上保存密钥

下载需要登录的模型（比如 Llama）要 Hugging Face 口令。在**那台节点**上：

```sh
hive node secret set HF_TOKEN       # 值从键盘输入，不会出现在命令行和历史里
```

Agent 只能按名字使用它，永远看不到值；默认只会发给 huggingface.co，输出里如果出现也会被打码。

### 4.7 给 Agent 发口令

在**中枢**上，为每个 Agent 单独建一个口令：

```sh
hive hub add-agent claude-desktop              # 会浏览网页、读陌生文件的 Agent 加 --cautious
```

它会打印口令（只显示一次）和要在 **Agent 那台电脑**上执行的一行命令，大致是：

```sh
hive init client --name home --url http://100.101.1.7:7700 --token - --default
```

运行后它会提示你粘贴口令（输入时不显示），然后自己把口令存进 `~/.hive/tokens/home.token`，并设成只有你能读（0600）。也可以用管道传进去，例如 `cat 口令文件 | hive init client --name home --url http://100.101.1.7:7700 --token -`。不要把口令写在命令行里（会留在 shell 历史里）。

`home` 是连接名（默认取中枢的名字），以它实际打印的为准。可以用 `--nodes 'desk,desk-wsl'` 限定这个口令能管哪几台电脑，`--role readonly|files|operator` 限定权限。

### 4.8 接上 Claude Desktop

在 Agent 那台电脑上：

```sh
hive mcp-config --client claude-desktop --connection home --write
```

（不写 `--connection` 时用 `client.json` 里的默认连接，并会告诉你用的是哪一个。）重启 Claude Desktop 即可。其他客户端见[第 5 节](#5-把-ai-助手接上来mcp)。

### 4.9 紧急情况

1. `hive hub stop`：一次切断所有经过中枢的 Agent。
2. 在每台电脑上 `hive node panic`：停掉所有任务、模型服务和 Docker 容器，并暂停节点。WSL 里的节点要在 WSL 里单独执行（Windows 上的 panic 会提示命令）；云机器在它自己的终端里执行（`/workspace/.hive/hive node panic`）。
3. 查清楚以后 `hive node resume` 恢复。

### 4.10 在聊天软件里用部署好的模型

见[第 6 节](#6-模型网关在聊天软件里用你部署的模型)。

---

## 5. 把 AI 助手接上来（MCP）

先确认 `client.json` 里有连接：本机节点是 `this-pc`（`hive init node` 自动加），中枢是你在 4.7 用 `hive init client` 加的名字。所有客户端都通过 `hive mcp-config` 生成配置，**口令不会写进客户端配置**（客户端运行 `hive mcp`，由它从 `~/.hive/tokens/` 读口令）。如果你用环境变量 `HIVE_HOME` 把数据目录放到了别处（第 12 节），`hive mcp-config` 会自动把 `HIVE_HOME` 写进配置的 `env`，客户端才能找到连接。

### Claude Desktop

```sh
hive mcp-config --client claude-desktop --connection home           # 只打印
hive mcp-config --client claude-desktop --connection home --write   # 直接写进配置文件（保留你已有的其他 MCP 服务器）
```

配置文件位置：Windows `%APPDATA%\Claude\claude_desktop_config.json`，macOS `~/Library/Application Support/Claude/claude_desktop_config.json`，Linux `~/.config/Claude/claude_desktop_config.json`（`--write` 会先把原文件备份成 `.bak`）。改完要**完全退出**再打开 Claude Desktop。如果改了没效果（比如从应用商店安装的版本配置文件在别处），在 Claude Desktop 里打开 设置 → 开发者 → 编辑配置，把不加 `--write` 时打印的 `"hive"` 那一段粘到 `mcpServers` 里。

### Claude Code

```sh
hive mcp-config --client claude-code --connection home
```

它打印一行 `claude mcp add hive -- … mcp --connection home --wait-budget 110`，复制到终端执行即可。

如果你想让 Claude Code 直接连中枢的远程 MCP（不在本机运行 `hive mcp`）：`hive mcp-config --client claude-code --connection home --remote`。注意这是**唯一会把口令写进 Agent 配置**的方式，一般用上面的本地方式就好。

### Cursor

```sh
hive mcp-config --client cursor --connection home --write    # 写进 ~/.cursor/mcp.json
```

或者不加 `--write`，把打印的内容放进项目里的 `.cursor/mcp.json`。

### 其他 MCP 客户端、自己写的程序

- `hive mcp-config --client generic` 打印通用的 stdio 配置；`hive mcp --help` 看全部参数（比如 `--profile minimal|standard|full` 控制工具多少，`--node desk-wsl` 设默认电脑）。
- `hive tools export --format openai`（或 `anthropic`、`mcp`、`openai-responses`）导出工具定义给你自己的程序。
- Python 客户端和示例：[examples/python_client.py](examples/python_client.py)、[examples/agent_anthropic.py](examples/agent_anthropic.py)（Claude 手写工具循环）、[examples/agent_openai.py](examples/agent_openai.py)（任何 OpenAI 兼容模型）、[examples/raw_http.sh](examples/raw_http.sh)、[examples/mcp/](examples/mcp/)。

接好以后，Agent 会看到 28 个工具（标准档），例如 `nodes_list`（有哪些电脑）、`shell_run`（执行命令，超过约 40 秒自动转成后台任务）、`job_start` / `job_logs`、`fs_read` / `fs_write` / `fs_edit`、`service_define` / `service_control`（托管模型服务：定义、启动、停止、重启）、`model_pull`（下载整个模型仓库）、`guide`（Agent 自己看的使用指南，比如 `guide('windows')`、`guide('model-serving')`）。

---

## 6. 模型网关：在聊天软件里用你部署的模型

### 6.1 先部署一个模型服务

一般直接让 Agent 来做（对它说“在 desk-wsl 上用 vLLM 部署 Qwen2.5-7B-Instruct”，它会用 `model_pull` 下载、`service_define` 部署）。你也可以自己用命令行定义一个**托管服务**：节点负责启动它、崩了自动重启、做健康检查、记录日志。`--node` 是目标电脑：

- 在**中枢那台电脑**上，`--node` 写的是别的电脑时，命令会自动改走中枢（屏幕上会提示 `using the hub connection 'hub-local'`），也可以自己写明 `--connection hub-local`；
- 在**其他电脑**上通过中枢操作时，加上你的中枢连接，例如 `--connection home`。

```sh
hive service define ollama --preset ollama --node desk                                   # Ollama（任何系统）；模型另外用 ollama pull 下载
hive service define ollama --preset ollama --node desk --connection hub-local            # 同上，在中枢那台电脑上写明走中枢
hive service define qwen --preset vllm --model Qwen/Qwen2.5-7B-Instruct --node desk-wsl   # vLLM（Linux / WSL）；--model 可以是本地文件夹
hive service define qwen-gguf --preset llamacpp --model ~/models/qwen2.5-7b-q4_k_m.gguf --node desk   # llama.cpp 的 GGUF 文件
```

- 预设的其他参数用 `--arg 键=值`（可重复），例如 `--arg python=~/.venvs/vllm/bin/python --arg max_model_len=32768`；每个预设有哪些参数：`hive guide presets`。
- 不用预设、自己写启动命令也行。想先不装显卡驱动、不下载模型就把整条路试通，可以用仓库自带的**假模型服务** `hive/examples/stub_openai.py`（只用 Python 自带的库，监听 8000 端口，回答 `stub reply: 你说的话`）。在仓库文件夹（`gongju`）里依次执行：

  ```sh
  hive push desk hive/examples/stub_openai.py '~/hive-test/stub_openai.py'     # 把它复制到 desk 的 ~/hive-test 文件夹（desk 就是本机也一样）
  hive service define stub --argv 'python3 stub_openai.py 8000' --cwd '~/hive-test' --port 8000 --endpoint http://127.0.0.1:8000/v1 --node desk
  hive call model.chat service=stub prompt=你好 --node desk                     # 应该看到 "content": "stub reply: 你好"
  ```

  `~` 两边的单引号不能省：这样 `~` 指的是 desk 上的主目录，而不是你正在打字的这台电脑的。desk 是 Windows 时把 `python3` 换成 `py`。试完删掉：`hive call service.remove name=stub --node desk`。

  `--argv` 是程序和参数（整体加引号），`--port` 是它监听的端口（用来做健康检查），`--endpoint` 是它的 OpenAI 兼容地址（网关和 `model_chat` 用它）。其他参数用 `--param 键=值`，例如 `--param health=tcp`。
- 命令会等最多 30 秒并打印状态（`state running, health healthy` 表示好了）。之后看日志：`hive logs desk --service qwen -f`；看状态：`hive call service.status name=qwen wait=120 --node desk`。
- `hive guide model-serving` 打印 Agent 用的部署说明（显存估算、下载、各种预设的写法）；`hive guide` 列出全部主题，内容和 Agent 的 `guide` 工具完全一样。

### 6.2 生成网关地址和专用口令

假设 `desk-wsl` 上已经有一个名叫 `qwen` 的 vLLM 服务（上一步部署的）。在**中枢那台电脑**上：

```sh
hive gateway token add chatbox --models 'desk-wsl/qwen'   # 生成一个“只能用这个模型”的专用口令（只显示一次）
hive gateway url desk-wsl qwen                            # 打印各种情况下该填的地址
```

`hive gateway url desk-wsl qwen` 会按情况打印几种地址：

| 地址 | 给谁用 |
| --- | --- |
| `http://100.x.y.z:7700/openai/desk-wsl/qwen/v1`（中枢的 Tailscale IP） | 装了 Tailscale 的电脑上的软件（推荐；家里直连的电脑能边生成边显示） |
| `http://127.0.0.1:7700/openai/desk-wsl/qwen/v1` | 中枢这台电脑上的软件 |
| `http://host.docker.internal:7700/openai/desk-wsl/qwen/v1` | 中枢这台电脑上 Docker 里的 Open WebUI |
| `https://<中枢>.ts.net/openai/desk-wsl/qwen/v1`（开了 Funnel 才有） | 网页版（https 页面）聊天软件、不在 Tailscale 里的设备 |

**Cherry Studio**：设置 → 模型服务 → 添加 → 类型选 “OpenAI”（OpenAI 兼容）→ API 地址填**不带末尾 `/v1`** 的地址（如 `http://100.x.y.z:7700/openai/desk-wsl/qwen`，Cherry Studio 会自己补 `/v1`）→ API 密钥填专用口令 → 在模型列表里添加模型名（部署时的模型名，`hive gateway url desk-wsl qwen --token-file 口令文件` 会把模型名打印出来）。

**Open WebUI**：管理员设置 → 外部连接（Connections）→ OpenAI API → 添加连接 → URL 填**带 `/v1`** 的地址（Docker 里运行就用 `host.docker.internal` 那个）→ Key 填专用口令 → 保存后在模型下拉框里就能看到。

**Chatbox 等其他软件**：选 “OpenAI 兼容 / 自定义 OpenAI”，Base URL 填带 `/v1` 的地址，API Key 填专用口令，模型名填部署时的名字。

注意：

- 软件走系统代理（Clash、v2rayN 的“系统代理”）时，要把 `100.64.0.0/10` 和 `*.ts.net` 加进代理的“绕过/直连”列表，否则连不上 Tailscale 地址。
- 云 GPU（中继）上的模型：回答会整段生成完再一次显示，照常可用。
- 你自己开着的 Ollama 托盘程序（11434 端口）和 LM Studio 也能走网关：服务名分别填 `ollama`、`lmstudio`，例如 `hive gateway token add phone --models 'desk/ollama'`。
- 这个口令只能调模型，不能执行命令、看文件；对话内容不写进审计日志。`hive gateway token list` 查看，`hive gateway token revoke chatbox` 吊销。
- 只想在单台节点上用（没有中枢）：`hive gateway token add chatbox --models 'qwen' --node`，地址用 `hive gateway url self qwen --node`。

---

## 7. 日常命令

所有命令都可以加 `--help` 查看参数。`self` 表示本机节点。在装了节点的电脑上，`hive sh` / `hive call` / `hive logs` / `hive push` / `hive pull` 默认连本机节点；要通过中枢操作别的电脑，加 `--connection home`（在中枢那台电脑上用 `--connection hub-local`），例如 `hive sh desk --connection hub-local -- nvidia-smi`。

| 想做什么 | 命令 |
| --- | --- |
| 看版本 | `hive version` |
| 本机节点状态 / 停止 / 重启 | `hive node status`、`hive node stop`、`hive node restart` |
| 中枢状态、节点列表 | `hive hub status`、`hive hub list` |
| 有哪些电脑在线 | `hive nodes`（在中枢那台电脑上默认只显示本机，看全部用 `hive nodes --connection hub-local`；它会提示你） |
| 在某台电脑上执行命令（退出码就是远端的退出码） | `hive sh desk -- nvidia-smi`（命令里有引号、管道时整体加引号：`hive sh desk -- 'ls ~ \| head'`） |
| 调用任意动作 | `hive call fs.stat path=~`、`hive call model.pull repo=Qwen/Qwen2.5-7B-Instruct --node desk-wsl` |
| 部署模型服务 | `hive service define qwen --preset vllm --model Qwen/Qwen2.5-7B-Instruct --node desk-wsl`（见 6.1） |
| 使用说明（和 Agent 看到的一样） | `hive guide`、`hive guide model-serving`、`hive guide presets` |
| 看所有动作 | `hive actions` |
| 跟踪任务 / 服务日志 | `hive logs desk j_xxxx -f`、`hive logs desk --service qwen -f` |
| 上传 / 下载文件（断点续传） | `hive push desk ./a.gguf ~/models/a.gguf`、`hive pull desk ~/out.txt ./out.txt` |
| 节点密钥 | `hive node secret set HF_TOKEN`、`hive node secret list`、`hive node secret unset HF_TOKEN` |
| 节点口令 | `hive node token list`、`hive node token add NAME`、`hive node token revoke NAME` |
| Agent 口令（中枢） | `hive hub add-agent NAME`、`hive hub remove-agent NAME` |
| 节点（中枢） | `hive hub add-node …`、`hive hub remove-node NAME`、`hive hub rotate-token NAME` |
| 审批 | `hive approvals list`、`hive approvals approve ap_xxx [--for 30m]`、`hive approvals deny ap_xxx` |
| 暂停 / 恢复 / 紧急停止 | `hive node pause`、`hive node resume`、`hive node panic` |
| 自检 | `hive doctor` |
| 审计日志 | `hive audit tail -n 50`（中枢的：`--hub`；持续跟踪：`--follow`） |
| 开机自启 | `hive service install node`、`hive service status node`、`hive service uninstall node`（中枢把 `node` 换成 `hub`） |

---

## 8. 安全：谨慎口令、审批、暂停和紧急停止

**基本规则**

- 每个请求都要口令；口令只存哈希；每个口令有角色：`readonly`（只读）、`files`（读写文件）、`operator`（全部，包括执行命令）。中枢上的 Agent 口令还能限定管哪几台电脑（`--nodes`）。
- 每台节点有自己的策略：允许访问的目录（`--root`）、禁止访问的路径、可以联网访问哪些地址。中枢连节点用的口令本身就是上限，中枢被攻破也越不过它。
- 默认只听 127.0.0.1；跨电脑走 Tailscale（加密）。所有操作写入审计日志（`hive audit tail`）。
- 密钥（HF_TOKEN 等）只存在那台节点上，Agent 只能按名字用。
- 没有执行权限的口令写不了会“自动运行”的文件（`.bashrc`、开机启动目录、Git 钩子等），Agent 也改不了 Hive 自己用的 Python。

**谨慎口令（cautious）和审批**

会浏览网页、读陌生文件的 Agent 可能被网页里藏的“指令”骗（提示注入）。给它们用谨慎口令：

```sh
hive hub add-agent claude-desktop --cautious        # 中枢上
hive init node --preset cautious                     # 或者整台节点都用谨慎档
```

已经发出去的口令也能随时改成谨慎口令，或改回来；口令本身不变，Agent 那边什么都不用改（`hive doctor` 提醒你时也会直接给出这条命令）：

```sh
hive node token set this-pc --cautious               # 节点上的口令（hive node token list 看名字）
hive hub set-agent claude-desktop --cautious         # 中枢上的 Agent 口令；--no-cautious 改回不需要审批
```

谨慎口令的**执行命令、启动服务、删除、移动、结束进程**会先停下来等你批准（Agent 会告诉你一个 `ap_…` 编号）：

```sh
hive approvals list                     # 看等待中的审批
hive approvals approve ap_xxxx          # 先完整显示要执行的内容（命令、环境变量、脚本文件内容），你确认后才放行
hive approvals approve ap_xxxx --for 30m    # 批准并在 30 分钟内自动放行同类操作
hive approvals deny ap_xxxx
hive approvals grants                   # 看已有的“30 分钟”授权；hive approvals revoke --all 全部收回
```

审批要在 Agent 连接的那台机器上做（用中枢就在中枢上）。不在终端里（脚本、远程执行）时没法回答 y/N，要加 `--yes`：`hive approvals approve ap_xxxx --yes`。

**批准以后要“重试”**：批准不会自动执行，而是让**同一个调用**带着这个编号再来一次。Agent 会自己重试（它知道带 `approval_id`）；你自己在命令行上被拦下时，命令会告诉你编号，批准后原样再执行一次并加上 `--approval`：

```sh
hive sh desk --approval ap_xxxx -- nvidia-smi                    # hive sh：命令必须和第一次完全一样
hive call shell.run command=nvidia-smi --approval ap_xxxx --node desk
```

注意：谨慎档**不拦写文件**，重要数据请放进只读目录（`--root 路径:ro`）。

**暂停与紧急停止**

| 命令 | 效果 |
| --- | --- |
| `hive node pause` | 暂停：Agent 只能“看”（读文件、查状态），不能执行、写入、启动任何东西，已批准的授权全部作废；正在跑的任务和服务不受影响 |
| `hive node resume` | 恢复（panic 停掉的东西不会自动重启） |
| `hive node panic` | 紧急停止：暂停 + 断开中继 + 结束所有任务、模型服务、Docker 容器。只作用于这一台电脑 |
| `hive hub stop` | 停掉中枢，一次切断所有经过中枢的 Agent |

`hive node pause` 会等正在运行的节点确认（最多 3 秒）后才返回 `paused (confirmed by the running node)`，之后发出的命令一定被拦下。节点进程没在运行时 `pause` / `panic` 也有效。`panic` 会先问你 y/N，脚本里用 `hive node panic --yes`。更多见 [docs/SECURITY.md](docs/SECURITY.md)。

**它不是木马**：必须你本人执行 `hive init node`（或 `hive init hub`）并输入 yes；启动时打印醒目横幅；系统服务名就叫 Hive；不会自我传播、不会隐藏。

---

## 9. 国内网络：代理与镜像

- **代理**：节点会自动使用你的 HTTP 代理（环境变量，或 Windows / macOS 的系统代理，比如 Clash 的 7890、v2rayN 的 10809）。只支持 HTTP 代理，SOCKS 请换成软件的 HTTP/混合端口。也可以手动指定：`hive init node --proxy http://127.0.0.1:7890`，关掉：`--proxy off`。本机、局域网、Tailscale 地址永远不走代理。
- **镜像**：`hive init node --mirror cn` 一键设置 Hugging Face 镜像（hf-mirror.com）和清华 PyPI；节点里启动的 pip、huggingface-cli、vLLM 也会继承代理和镜像设置。
- **WSL 的 NAT 网络**：Windows 主机地址每次重启都会变，代理写成 `--proxy http://wsl-host:7890`，Hive 会自动找到当前地址（Clash 要打开“允许局域网”）；用 mirrored 网络就可以直接写 `http://127.0.0.1:7890`。
- **Docker** 拉镜像需要在 Docker 自己的设置里配镜像源或代理。

---

## 10. 出问题了怎么办（hive doctor）

先运行自检，它会逐项检查并给出修复方法（`fix:` 那一行）：

```sh
hive doctor              # 自动判断本机是节点、中枢还是客户端；也可以 hive doctor node / hub / client
hive doctor --deep       # 更深入的检查
hive doctor --no-network # 不做联网检查
```

常见问题：

| 现象 | 原因 / 解决 |
| --- | --- |
| `hive: command not found`（Linux / WSL / macOS） | 虚拟环境没激活：用 `~/.venvs/hive/bin/hive`，或按 2.2 建 `~/.local/bin/hive` 链接后重开终端 |
| `hive` 不是内部或外部命令（Windows） | 重开 PowerShell；还不行就重新安装 Python 并勾选 “Add python.exe to PATH” |
| `unavailable: cannot connect to http://127.0.0.1:7701` | 节点没在运行：`hive node run`，或 `hive service install node`；`hive node status` 看原因 |
| 节点启动报 `refusing to run as root/Administrator` | 不要用 root / 管理员运行，换普通用户。只有 root 是唯一用户的云容器才在节点配置里写 `"allow_root": true`（`hive join` 在云机器上会自动写） |
| Claude Desktop 里看不到 Hive 工具 | 完全退出再打开 Claude Desktop；用 `hive mcp-config --client claude-desktop` 核对配置；`hive doctor client` |
| 中枢连不上家里的某台电脑 | 那台电脑要 `hive init node --tailscale`；Windows 防火墙放行（`hive doctor` 打印命令）；两台电脑 `tailscale status` 都在线 |
| 改了配置后中枢 / 节点“停了” | 在终端窗口里前台运行的会自动重启（`restarting to apply new settings…`）。如果是在后台、没有终端的方式手动启动的（例如 `nohup hive hub run &`），它会停下并提示你 `start it again: hive hub run`；长期使用请装成服务：`hive service install hub`（节点：`hive service install node`） |
| 中枢里本机节点（`hubhost`）一直 offline | 本机节点没加载中枢口令：`hive node restart`（`hive init hub --with-local-node` 现在会自动做这一步） |
| `no connection 'home' in client.json`（Claude Desktop 里） | 你改过 `HIVE_HOME`：重新执行 `hive mcp-config --client claude-desktop --connection home --write`，它会把 `HIVE_HOME` 写进配置 |
| 聊天软件连不上网关地址 | 软件走了系统代理：把 `100.64.0.0/10`、`*.ts.net` 加进代理的直连列表；https 网页版软件要用 Funnel 的 https 地址 |
| Windows 上升级报 `WinError 32` | 先退出 Claude Desktop、Cursor、Claude Code（它们在运行 `hive.exe`），再 `py -m pip install -U .\hive` |
| 云机器显示“换了一台机器注册” | 见 4.5：不是你换的就 `hive hub rotate-token NAME`，是你新开的才 `hive hub ack-node NAME` |
| WSL 里的节点过一会儿就掉线 | Windows 节点要有 keepalive：`hive init node --wsl-keepalive Ubuntu-24.04`（在 Windows 上执行） |
| 下载模型很慢 / 失败 | 国内用 `--mirror cn` 和代理（第 9 节）；`hive logs 节点 任务号 -f` 看进度 |

更深的原理和所有细节见 [docs/DESIGN.md](docs/DESIGN.md)（例如 Windows 见 §20.5，WSL2 见 §20.7）。

---

## 11. 升级与卸载

**升级**（在仓库里先 `git pull` 或重新下载），按你当初的安装方式选一行：

```sh
~/.venvs/hive/bin/pip install -U ./hive   # Linux / macOS / WSL（按 2.2 / 2.3 / 2.4 装在虚拟环境里的，大多数人是这种）
```

```powershell
py -m pip install -U .\hive               # Windows：先完全退出 Claude Desktop / Cursor / Claude Code（它们在运行 hive.exe）
```

- 用单文件 `hive.pyz` 的：重新生成或下载一份新的 `hive.pyz` 覆盖旧文件即可。
- **不要**在 Linux / macOS 上用 `python3 -m pip install -U ./hive`：它会往系统 Python 再装一份 Hive，并覆盖 `~/.local/bin/hive` 这个链接（新版 Ubuntu 上还会直接报 externally-managed 错误）。

然后让正在运行的程序换上新版本：

```sh
hive node restart                        # 本机节点
hive hub restart                         # 中枢那台电脑
hive hub restart-node --outdated         # 云机器从中枢自动下载新版本并重启（不用再一台台打开网页终端）
```

`restart-node --outdated` 只会自动升级“云机器”（用 `hive join --install` 或 `start-hive.sh` 跑起来、中继角色是 `operator` 的节点）。其他过期的节点它会逐个说明原因（例如 `not a cloud pod`、`relay role is 'files'`），并告诉你怎么办：家里的电脑照上面的方法手动升级后 `hive node restart`；角色被收窄的云机器可以在那台机器上执行 `hive init node --relay-role operator --auto-update` 打开自动升级。

后台任务和模型服务在节点重启、升级时**继续运行**，回来后自动接管。

**卸载**（先停掉并移除开机自启，再删程序）：

```sh
hive mcp-config --client claude-desktop --remove   # 先把 Hive 从 Claude Desktop 的配置里拿掉（Cursor 用 --client cursor）
hive mcp-config --client claude-code --remove      # Claude Code：它会告诉你要执行的 claude mcp remove hive
hive service uninstall node --stop-services   # 同时停掉托管的模型服务和后台任务（不加它们会一直跑下去）
hive service uninstall hub               # 只有中枢那台电脑需要
rm -rf ~/.venvs/hive ~/.local/bin/hive   # Linux / macOS / WSL：删掉虚拟环境和 hive 链接
rm -rf ~/.hive                           # 可选：配置、口令、日志、审计都在这里，删了就彻底清干净
```

```powershell
hive mcp-config --client claude-desktop --remove
hive service uninstall node --stop-services
py -m pip uninstall hive-agent           # Windows
Remove-Item -Recurse -Force $HOME\.hive  # 可选：删除数据目录
```

用 `hive.pyz` 的：`python3 hive.pyz service uninstall node --stop-services` 之后删掉 `hive.pyz` 和数据目录（云机器上是 `/workspace/.hive`）。

`--remove` 只动 Hive 自己那一条（`"hive"`），别的 MCP 服务器原样保留，旧文件备份成 `.bak`。删完要**完全退出**再打开 Claude Desktop / Cursor。卸载前忘了这一步也没关系：手动打开它们的配置文件（Claude Desktop：设置 → 开发者 → 编辑配置；Cursor：`~/.cursor/mcp.json`），删掉 `"hive"` 那一段即可；项目自己的 `.cursor/mcp.json` 里如果也加过，也要删。

---

## 12. 目录结构

**本文件夹（源码）**

```text
hive/
├─ README.md          本说明（给使用者）
├─ pyproject.toml     安装描述（pip install ./hive，包名 hive-agent，命令 hive）
├─ docs/
│  ├─ DESIGN.md       完整设计文档（开发者参考，所有细节以它为准）
│  ├─ SECURITY.md     安全模型与加固指南
│  ├─ PROTOCOL.md     HTTP / 中继协议
│  └─ PHASE0_NOTES.md 开发过程中的接口约定记录
├─ hive/              Python 源码（纯标准库）
│  ├─ cli.py          命令行 hive
│  ├─ node.py         节点运行时        hub.py / relay.py   中枢与中继
│  ├─ mcp.py          MCP 服务器        tools.py / render.py / guides.py   给 Agent 的工具层
│  ├─ gateway.py      模型网关          actions/            38 个动作的实现
│  └─ …               策略、口令、审计、进程、文件、下载、安装等模块
├─ tests/             自动测试（tests/integration 是端到端测试）
├─ examples/          接入示例（Python、OpenAI / Anthropic 工具循环、MCP 配置、curl）
└─ deploy/            systemd / launchd / Windows 计划任务 / Caddy / RunPod 启动脚本模板
```

**你电脑上的数据目录 `~/.hive`**（可用环境变量 `HIVE_HOME` 改到别处；云机器上是 `/workspace/.hive`）

```text
~/.hive/
├─ client.json        连接列表（this-pc、hub-local、home …），hive mcp / hive call 用它
├─ tokens/            口令文件（this-pc.token、this-pc-owner.token、hub-owner.token …），只有你能读
├─ node/
│  ├─ config.json     节点配置
│  ├─ audit/          审计日志（每天一个文件）
│  ├─ logs/node.log   节点运行日志
│  ├─ state/          任务、服务、日志等运行状态
│  └─ secrets.json    节点密钥
└─ hub/
   ├─ config.json     中枢配置（节点、Agent 口令、网关口令）
   └─ state/          中继节点身份等
```

**开发者**：运行测试 `cd hive && python3 -m unittest discover -s tests -t .`；生成单文件 `python3 -m hive dist build --out hive.pyz`。

---

## 13. English quick start

Hive lets AI agents (MCP clients, OpenAI/Anthropic tool loops, scripts) operate machines you own: run commands, edit files, background jobs, processes, model servers (vLLM, Ollama, llama.cpp, ComfyUI, Docker), and use deployed models through an OpenAI-compatible gateway. Stdlib-only Python 3.9+. Design reference: [docs/DESIGN.md](docs/DESIGN.md).

```sh
pip install ./hive                               # or: python3 -m hive dist build --out hive.pyz; python3 hive.pyz …
hive init node && hive node run                  # one machine
hive mcp-config --client claude-desktop --write  # MCP for Claude Desktop (--client claude-code / cursor)

hive init hub --tailscale --with-local-node && hive hub run   # several machines
hive hub add-node desk --direct http://100.101.1.5:7701 --generate
hive hub add-node pod-a --relay                  # join code / one-liner for a NAT'd GPU pod
hive hub add-agent claude --cautious             # an agent token for another machine
hive init client --name home --url http://100.101.1.7:7700 --token - --default   # on the agent's machine (paste)
hive service define qwen --preset vllm --model Qwen/Qwen2.5-7B-Instruct --node desk   # a model server
hive guide model-serving                         # the agents' how-to topics
```

A foreground `hive node run` / `hive hub run` in a terminal restarts itself in place when a command changes its
config. Upgrade a venv install with `~/.venvs/hive/bin/pip install -U ./hive`; uninstall with
`hive service uninstall node --stop-services` then `rm -rf ~/.venvs/hive ~/.local/bin/hive` (and `~/.hive` for the data).

Python:

```python
import hive
with hive.connect("home") as c:                 # a client.json connection
    print(c.call("shell.run", {"command": "nvidia-smi", "timeout": 30}, node="desk-wsl")["stdout"])
    tk = c.toolkit(wait_budget=90)              # tk.anthropic_tools(), tk.openai_tools(), tk.call(name, args)
```

Examples: [examples/agent_anthropic.py](examples/agent_anthropic.py) (Claude, manual tool loop; it opts into the API's server-side refusal fallback — remove `betas`/`fallbacks` if you do not want it), [examples/agent_openai.py](examples/agent_openai.py), [examples/python_client.py](examples/python_client.py), [examples/raw_http.sh](examples/raw_http.sh), [examples/mcp/](examples/mcp/). Security model: [docs/SECURITY.md](docs/SECURITY.md).
