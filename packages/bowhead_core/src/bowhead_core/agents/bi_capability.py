"""Identify BI-capable peers from their tool whitelist.

Keep this predicate outside the prompts package because it recognizes historical
tool names that are hidden from models. Disabling a builtin must not remove data
routing when the current roster contains a capable custom Agent.
"""

from __future__ import annotations

from typing import Any


def tools_can_fetch_bi(tool_names: Any) -> bool:
    """工具名集合里有没有取数工具。`None` / 不可迭代 → False（判不出就当没有）。"""
    if not tool_names:
        return False
    try:
        names = tuple(tool_names)
    except TypeError:
        return False
    from bowhead_core.agents.capability_roles import agent_capabilities

    return "cap_data_query" in agent_capabilities(names)


def profile_can_fetch_bi(name: str) -> bool:
    """内置 peer 的工具白名单里有没有取数工具。

    读 `PROFILES`（进程内常量，无 IO）。取不到该 profile → False：宁可让取数规则
    回落到 `compute` 那条老路，也不要把取数派给一个其实没有 `bi_query` 的 peer
    —— 那比"说没有能力"更糟，它会去编。
    """
    from bowhead_core.subagent import PROFILES

    prof = PROFILES.get(name)
    if prof is None:
        return False
    return tools_can_fetch_bi(getattr(prof, "tool_whitelist", ()) or ())
