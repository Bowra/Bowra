"""批 3 的开关 —— Lead 带 turn 级历史（`BOWHEAD_CTX_TURN_HISTORY`）。

**默认 ON**（tech-design r3，用户拍板：成本是算术题不是未知数）。
回滚绳：`BOWHEAD_CTX_TURN_HISTORY=0` ⇒ 逐字节回到 digest 形态，无迁移、无状态。

历史有上限（`BOWHEAD_CTX_TURN_HISTORY_TOKENS`，默认约 40k token）：超了掉最老的**整轮**、
绝不切轮内文本；掉到 0.75 × 上限为止（滞回——每隔几轮才作废一次前缀缓存，不是每轮都作废）。
上限不是为常态设的（dev 库口径历史通常 1–3k），是为 fleet 128k 那档留的余量。

⚠ 每个 turn 只读一次（lead_orchestrator 构造 H 的那一刻）；循环中途翻转不影响本 turn。
"""

from __future__ import annotations

import os

_ENV = "BOWHEAD_CTX_TURN_HISTORY"
_ENV_TOKENS = "BOWHEAD_CTX_TURN_HISTORY_TOKENS"
DEFAULT_HISTORY_TOKENS = 40_000
HISTORY_HYSTERESIS = 0.75


def turn_history_enabled() -> bool:
    """Lead 是否带 turn 级历史。默认 ON；显式 set=0 为 kill-switch。"""
    return os.environ.get(_ENV, "1").strip() == "1"


def turn_history_token_budget() -> int:
    """历史软预算（token）。垃圾 / ≤0 回退默认值。"""
    raw = os.environ.get(_ENV_TOKENS, "").strip()
    try:
        v = int(raw) if raw else DEFAULT_HISTORY_TOKENS
    except ValueError:
        return DEFAULT_HISTORY_TOKENS
    return v if v > 0 else DEFAULT_HISTORY_TOKENS


__all__ = ["DEFAULT_HISTORY_TOKENS", "HISTORY_HYSTERESIS", "turn_history_enabled", "turn_history_token_budget"]
