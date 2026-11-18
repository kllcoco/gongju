#!/usr/bin/env python3
"""The Hive Python client without an LLM (design §15, §22.4): run a command, start a job and follow it, copy a file.

    python python_client.py [--connection home] [--node desk-wsl]
"""
from __future__ import annotations

import argparse

import hive


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection")
    ap.add_argument("--node", help="node name on a hub (omit for a direct node)")
    a = ap.parse_args()
    with hive.connect(a.connection, default_node=a.node) as c:
        for n in c.nodes():
            print(n["name"], "online" if n.get("online") else "offline", n.get("os_label"), n.get("gpus_summary"))
        r = c.call("shell.run", {"command": "python3 --version || python --version", "timeout": 30})
        print("exit", r["exit_code"], (r.get("stdout") or r.get("output") or "").strip())
        node = c.node()
        job = node.job(c.call("job.start", {"command": "echo start && sleep 2 && echo done"})["job_id"])
        print("job", job.job_id, job.wait(timeout=60)["state"])
        for chunk in node.follow_logs(job_id=job.job_id):
            print(chunk, end="")
        info = c.call("system.info", {"sections": ["gpu", "disk"]})
        print("gpus:", [g.get("name") for g in info.get("gpus") or []])
        # copy a file to the node (resumable, sha256-verified); local paths need the local: prefix
        # print(c.transfer("local:./model.gguf", f"{a.node or 'self'}:~/models/model.gguf"))


if __name__ == "__main__":
    main()
