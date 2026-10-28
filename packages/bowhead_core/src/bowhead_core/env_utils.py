"""环境变量解析小工具.

2026-06-12 统一前 repo 里已有 6 份近似拷贝的「读 env → float → 失败给默认值」
（tool_finding / tool_registry / bi_concept_resolve 各一个具名 helper +
subagent ×2 / lead_orchestrator 各 inline `float(os.environ.get(...))`），
各自漂移：有的没 >0 clamp（负值传给 wait_for = 立刻超时）、inline 版垃圾值
直接在 turn 运行时抛 ValueError。收口到这一个实现。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Warn only once per variable on parse failures/clamping: these helpers are called repeatedly
# on the per-LLM-call hot path, so misconfiguration must not flood logs. In-process state resets on restart.
_warned: set[str] = set()


def _warn_once(name: str, msg: str) -> None:
    if name in _warned:
        return
    _warned.add(name)
    logger.warning(msg)


def env_float(name: str, default: float) -> float:
    """读环境变量解析成 float；未设/空/解析失败/≤0/NaN → default（带一次性 warning）.

    这些值几乎全用作 timeout/budget：0、负数、NaN 传给 asyncio.wait_for 等于
    立刻超时，从来不是本意 → 一律回退 default。inf 放行（= 显式禁用上限，与
    统一前各站点的 float() 透传行为一致）。注意 `not (v > 0)` 而非 `v <= 0`：
    NaN 与任何数比较都是 False，后者会把 NaN 漏过去。
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _warn_once(name, f"env_float: {name}={raw!r} 解析失败 → 用默认 {default}")
        return default
    if not (v > 0):
        _warn_once(name, f"env_float: {name}={v!r} 非正值(timeout/budget 语义) → 用默认 {default}")
        return default
    return v
