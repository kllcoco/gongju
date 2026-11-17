"""灵枢 LingShu — let any AI agent operate any machine that deploys and runs systems and models.

Architecture (all stdlib, Python 3.9+):

* Node  (``lingshu node``): a small daemon on every controlled machine. It executes
  actions (shell, background jobs, files, processes, managed services, GPU info,
  local HTTP calls to model servers) under a local policy, and either listens for
  HTTP (direct mode) or dials out to a hub and pulls tasks (relay mode, works
  behind NAT / on cloud GPU pods without open ports).
* Hub   (``lingshu hub``): optional central server. Agents talk to one URL; the hub
  routes calls to direct nodes, relay nodes, or an embedded local node.
* Adapters: MCP stdio server (``lingshu mcp``), OpenAI / Anthropic tool schemas,
  a Python client and a CLI — so any agent framework can plug in.
"""

__version__ = "1.0.0"
