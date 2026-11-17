"""Text renderers for tool results (design §17.6, §4.6, WP6).

Phase 0: ``RenderCtx`` is real (§4.6); ``generic`` is a placeholder that prints the result as JSON (WP6 replaces it
with the §17.6 rules). This module is one of the two places allowed to use ``util.iso()`` (local display).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class RenderCtx:
    """What a renderer knows about the call."""
    tool: str
    node: Optional[str]
    node_mode: str
    args: dict
    meta: dict
    char_budget: int


def generic(result: dict, args: dict, ctx: RenderCtx) -> str:
    """Fallback renderer (P0 placeholder: compact JSON with a node header)."""
    head = f"[{ctx.node}] " if ctx.node else ""
    text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    if ctx.char_budget and len(text) > ctx.char_budget:
        text = text[:ctx.char_budget] + f"\n…[{len(text) - ctx.char_budget} chars omitted]"
    return f"{head}{ctx.tool}\n{text}"


__all__ = ["RenderCtx", "generic"]
