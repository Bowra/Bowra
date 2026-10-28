"""批 2 的总开关 —— system 前缀按波动率重排（`BOWHEAD_CTX_PREFIX_ORDER`）。

三处改动共用**同一个** flag，因为它们是同一件事的三个面，分开翻会让 A/B 读不出因果：

- 2-a `SubagentRunner` 的 system 分层：per-turn 段落从 system 搬到 messages 末尾；
- 2-b `build_lead_system_prompt` 的 sections 内部按波动率重排；
- 2-c `advertised_tool_schemas` 出口按 name 排序（tools 在 render order 的位置 0）。

**默认 ON**（[[feedback_design_no_hard_caps]]：新行为默认打开，让真流量跑出真信号）。
回滚绳：`BOWHEAD_CTX_PREFIX_ORDER=0` ⇒ 三处都逐字节回到改前形态，无迁移、无状态。

⚠ **每次 run 只读一次**：若在一次 peer 循环中途翻转，system 会在循环内变形——
那正是这批要治的病（`turn-latency` 实施 A 踩过同一个坑，见 `subagent.py` 里 `_refs_in_messages` 的注释）。
"""

from __future__ import annotations

import os

_ENV = "BOWHEAD_CTX_PREFIX_ORDER"


def prefix_order_enabled() -> bool:
    """system/tools 是否按波动率排序。默认 ON；显式 set=0 为 kill-switch。"""
    return os.environ.get(_ENV, "1").strip() == "1"


__all__ = ["prefix_order_enabled"]
