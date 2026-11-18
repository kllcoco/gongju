#!/usr/bin/env python3
"""Hive + Claude: a manual tool-use loop with the official Anthropic SDK (design §22.4).

    pip install anthropic            # the example's only dependency; Hive itself is stdlib-only
    export ANTHROPIC_API_KEY=...     # or `ant auth login`
    python agent_anthropic.py "Check the GPUs on desk-wsl and start Qwen2.5-7B with vLLM" [--connection home] [--yes]

The Hive connection comes from client.json (`hive init client …`, or `this-pc` on a node). Tool calls of one turn run
in parallel and all their results go back in ONE user message. Before any tool whose annotations say
`destructiveHint`, the script asks y/N (pass --yes to skip). Refusal fallback: the request opts into the server-side
fallback beta (`fallbacks="default"`), so a declined turn is re-run on a fallback model inside the same call; remove
the `betas`/`fallbacks` arguments (and use `client.messages.create`) if you do not want that.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import anthropic

import hive
from hive.tools import Toolkit

MODEL = "claude-opus-5-5"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--connection", help="client.json connection (default: its default)")
    ap.add_argument("--yes", action="store_true", help="do not ask before destructive tools")
    a = ap.parse_args()

    hc = hive.connect(a.connection)
    tk = Toolkit(hc, wait_budget=90)          # no MCP time limit here; HttpTransport 120 s (§17.4)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": a.task}]

    def run_tool(block):
        spec = tk.get_tool(block.name)
        if spec is not None and spec.annotations.get("destructiveHint") and not a.yes:
            ans = input(f"Allow {block.name}({block.input})? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                        "content": "The user declined this call."}
        r = tk.call(block.name, block.input)
        return {"type": "tool_result", "tool_use_id": block.id, "content": r.text, "is_error": r.is_error}

    while True:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=tk.instructions(),
            tools=tk.anthropic_tools(),       # tool_choice stays auto (forced tool choice is rejected by this model)
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # check stop_reason before reading content
        if response.stop_reason == "refusal":
            print("The request was declined (refusal).", file=sys.stderr)
            return 1
        if response.stop_reason == "max_tokens":
            print("Stopped at max_tokens; ask for a shorter answer or raise max_tokens.", file=sys.stderr)
            return 1
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":         # end_turn / stop_sequence
            for b in response.content:
                if b.type == "text":
                    print(b.text)
            return 0
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]
        for b in uses:
            print(f"→ {b.name} {b.input}", file=sys.stderr)
        if a.yes:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(run_tool, uses))
        else:                                           # y/N prompts one at a time
            results = [run_tool(b) for b in uses]
        messages.append({"role": "user", "content": results})   # all tool_result blocks in ONE user message


if __name__ == "__main__":
    sys.exit(main())
