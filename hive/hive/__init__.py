"""蜂巢 Hive — let any AI agent operate any machine that deploys and runs systems and models (design §2, §3).

Architecture (all stdlib, Python 3.9+):

* Node  (``hive node``): a small daemon on every controlled machine. It executes
  actions (shell, background jobs, files, processes, managed services, GPU info,
  local HTTP calls to model servers) under a local policy, and either listens for
  HTTP (direct mode) or dials out to a hub and pulls tasks (relay mode, works
  behind NAT / on cloud GPU pods without open ports).
* Hub   (``hive hub``): optional central server. Agents talk to one URL; the hub
  routes calls to direct nodes or relay nodes, and serves the model gateway.
* Adapters: MCP stdio server (``hive mcp``), OpenAI / Anthropic tool schemas,
  a Python client and a CLI — so any agent framework can plug in.

``hive.connect`` is a lazy re-export of ``hive.client.connect`` (importing ``hive`` stays cheap).

D56: this file imports nothing at module level (not even ``__future__``/``typing``): ``python -m hive`` imports it
before ``hive.__main__`` can prune a writable cwd from ``sys.path``.
"""
__version__ = "1.0.0"
API_VERSION = 1


def __getattr__(name: str) -> "object":
    """Lazy ``connect`` re-export (PEP 562)."""
    if name == "connect":
        from .client import connect
        return connect
    raise AttributeError(f"module 'hive' has no attribute {name!r}")


__all__ = ["__version__", "API_VERSION", "connect"]
