#!/usr/bin/env python3
"""Hive + any OpenAI-compatible Chat Completions model, stdlib only (design §22.4).

    export OPENAI_API_KEY=...  OPENAI_MODEL=gpt-…            # or any compatible server:
    export OPENAI_BASE_URL=https://<hub>/openai/<node>/<service>/v1  OPENAI_API_KEY=<a Hive gateway token>
    python agent_openai.py "How much free disk and VRAM does desk-wsl have?" [--connection home]

OPENAI_BASE_URL may point at a model deployed with Hive itself through the model gateway (§8.7; the key is then a
`models` token from `hive gateway token add`). Tool calls are dispatched with `tk.call(name, arguments_json_text)`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

import hive
from hive.tools import Toolkit


def chat(base: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--connection")
    ap.add_argument("--max-turns", type=int, default=30)
    a = ap.parse_args()
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL")
    if not model:
        print("set OPENAI_MODEL", file=sys.stderr)
        return 2
    tk = Toolkit(hive.connect(a.connection), wait_budget=90)
    messages = [{"role": "system", "content": tk.instructions()}, {"role": "user", "content": a.task}]
    for _ in range(a.max_turns):
        resp = chat(base, key, {"model": model, "messages": messages, "tools": tk.openai_tools(),
                                "tool_choice": "auto"})
        msg = resp["choices"][0]["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            print(msg.get("content") or "")
            return 0
        for c in calls:
            fn = c["function"]
            print(f"→ {fn['name']} {fn.get('arguments')}", file=sys.stderr)
            r = tk.call(fn["name"], fn.get("arguments") or "{}")   # JSON text is parsed (and validated) by Hive
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": r.text})
    print("stopped after --max-turns", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
