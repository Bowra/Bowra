"""工具改名的读路径规范化——所有按工具名做判定的地方的**唯一**入口。

权威规范：`docs/design/工具改名迁移.md`。

为什么是「读路径规范化」而不是「数据迁移」：客户各自实例里有大量自建 agent 引用
被改名的工具，其中相当一部分把工具名写进了 `system_prompt` **正文**——那是客户的
自由文本，我们看不到也改不了。所以一个字节的客户数据都不动，旧名只在**解析时**
被归一成现名。别名是**永久的**，不是过渡期。

映射表由 `tool_aliases.yaml` 的 `renamed_from` 经 codegen 产出
（`agents/_generated/tool_renames.py`）。**不要**在别处再建一张表。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bowhead_core.agents._generated.tool_renames import (
    TOOL_RENAMES,
    canonical_tool_name,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_RENAMES",
    "canonical_tool_name",
    "canonicalize_tool_calls",
    "canonicalize_tool_refs",
]


class _NamedToolCall(Protocol):
    name: str


def canonicalize_tool_calls[T: _NamedToolCall](tool_calls: list[T] | None, *, source: str) -> list[T]:
    """C1 收口：把 LLM 吐出来的 tool call 名**就地**归一。

    必须在任何按名字做的判定**之前**跑——白名单闸 / dispatch guard / 熔断 /
    `step_started` 事件全都早于 `ToolRegistry.execute`，只在 execute 里归一会全漏。

    就地改 `tc.name`（而不是返新对象）：`messages` 历史、事件 payload、
    `ToolInvocation.tool_name` 都从同一个对象取名，改一处即处处一致
    （chat 不变量：live / 持久化 / 重放三者必须一致）。

    `source` 只进遥测标签，用来回答「旧名还在不在被模型照着调、来自哪条 loop」。
    """
    if not tool_calls or not TOOL_RENAMES:
        return tool_calls or []
    for tc in tool_calls:
        old = getattr(tc, "name", None)
        if not isinstance(old, str):
            continue
        new = TOOL_RENAMES.get(old)
        if new is None:
            continue
        tc.name = new
        _record_rename_hit(old=old, new=new, source=source)
    return tool_calls


def canonicalize_tool_refs(refs: Any, *, source: str) -> list[str]:
    """C2/C3/C6/C7 收口：把配置面（自建 agent 的 `tool_refs`）里的旧名归一。

    去重但**保序**——`tool_refs` 顺序会进提示词，抖动会打掉 prompt cache 前缀。
    `teammcp.*` / 其他非改名的名字原样通过。
    """
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs or []:
        name = str(ref or "").strip()
        if not name:
            continue
        new = TOOL_RENAMES.get(name)
        if new is not None:
            _record_rename_hit(old=name, new=new, source=source)
            name = new
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _record_rename_hit(*, old: str, new: str, source: str) -> None:
    """弃用遥测。没有这份数据就永远不知道旧名还在不在被真的用。

    ⚠ 绝不能因为遥测本身出问题而弄挂一次调用——工具分发是 hot path。
    """
    # ★ Both logs **and** metrics are required: `/metrics` is off by default (`BOWHEAD_METRICS_ENDPOINT_ENABLED`);
    # 116 returned 404, so counters alone make this data **unavailable** in production. Its entire purpose
    # is to make removing old names safe. Logs require no flag and can be searched in container logs.
    # (Unit tests cannot reveal this: they read the in-process registry successfully. 116 dogfood exposed it.)
    logger.info("tool_name.rename_resolved old=%s new=%s source=%s", old, new, source)
    try:
        from bowhead_core import metrics as _m

        _m.inc(
            "bowhead_tool_name_rename_total",
            labels={"old": old, "new": new, "source": source},
            help="工具旧名被解析成现名的次数（按旧名/现名/来源面）。弃用遥测：没有它就永远不知道旧名还在不在被真的用。",
        )
    except Exception:  # noqa: BLE001 — Telemetry failure must not affect dispatch.
        logger.debug("tool_name_canon.metric_failed old=%s", old, exc_info=True)
