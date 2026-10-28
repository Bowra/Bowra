"""Recover narrowly recognized tool-call markup from prose.

Callers enable this only when no structured tool calls exist. Require an invoke
name and at least one parameter; parse each value as JSON when possible, otherwise
retain its raw string for tool-side validation. Match the supported invoke and
parameter structure independently of vendor names. Never replace real calls or
interpret ordinary prose as a tool invocation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# `<arbitrary short marker invoke name="tool">`: allow a 24-character prefix for variants such as `<｜DSML｜` and `<|tool|>`.
_INVOKE_RE = re.compile(r"<[^>\n]{0,24}invoke\s+name=\"([A-Za-z_][A-Za-z0-9_]{0,63})\"", re.S)
# `<arbitrary short marker parameter name="key" ...>value`: read until the next closing tag or parameter.
_PARAM_RE = re.compile(
    r"<[^>\n]{0,24}parameter\s+name=\"([A-Za-z_][A-Za-z0-9_]{0,63})\"[^>\n]{0,64}>",
    re.S,
)
_CLOSE_RE = re.compile(r"</[^>\n]{0,24}parameter")


def _coerce(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    # Decode quoted JSON string literals too, or `"x"` is passed to the tool with its quotes intact.
    if raw[0] in '{["' or raw in ("true", "false", "null") or raw.lstrip("-").replace(".", "", 1).isdigit():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def salvage_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """从正文里解析出 ``[{"name":..., "arguments": {...}}]``；认不出返 ``[]``.

    返回的是**朴素 dict**，不 import router 的 ToolCall —— 保持这个模块零依赖、可单测。
    """
    if not content or "invoke" not in content:
        return []
    out: list[dict[str, Any]] = []
    for m in _INVOKE_RE.finditer(content):
        name = m.group(1)
        # This invoke's scope ends before the next invoke.
        nxt = _INVOKE_RE.search(content, m.end())
        body = content[m.end() : (nxt.start() if nxt else len(content))]
        args: dict[str, Any] = {}
        for pm in _PARAM_RE.finditer(body):
            key = pm.group(1)
            rest = body[pm.end() :]
            close = _CLOSE_RE.search(rest)
            nxt_param = _PARAM_RE.search(rest)
            end = min(x.start() for x in (close, nxt_param) if x) if (close or nxt_param) else len(rest)
            args[key] = _coerce(rest[:end])
        if args:
            out.append({"name": name, "arguments": args})
    return out


def apply_text_tool_call_salvage(resp: Any, *, make_tool_call: Any) -> list[str]:
    """就地把 ``resp`` 里被写成正文的工具调用转成结构化 tool_calls；返回被救回的工具名.

    调用方只需一行。**只在 ``resp.tool_calls`` 为空时动手** —— 永远不和真调用竞争。
    救回后清空 ``content``：那串标记本身不是给用户看的答案，留着会被当正文渲染出去。

    ``make_tool_call(id, name, arguments)`` 由调用方注入，避免本模块依赖 router 类型。
    """
    if getattr(resp, "tool_calls", None):
        return []
    salvaged = salvage_text_tool_calls(getattr(resp, "content", "") or "")
    if not salvaged:
        return []
    resp.tool_calls = [make_tool_call(f"salvaged_{i}", c["name"], c["arguments"]) for i, c in enumerate(salvaged)]
    resp.content = ""
    return [c["name"] for c in salvaged]
