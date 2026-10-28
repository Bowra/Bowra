"""Planning Mode flag gate — 共用,避免 plans.py/chat.py 各定义一份(C1 去重)。"""

from __future__ import annotations

import os


def planning_enabled() -> bool:
    """Planning flag (BOWHEAD_PLANNING_MODE)。默认 ON(0.71.5 事故修复+真用户验证后重开);
    set=0 秒级 kill。用于:authed plan 路由(skip/cancel/state/edit/amend)+ chat/send 的
    approve-via-send(track D)守卫门。dev-double-gate(dev body-id 路由,额外要 AUTH_MODE=dev)
    在 plans._dev_enabled,不在此。"""
    return os.environ.get("BOWHEAD_PLANNING_MODE", "1") == "1"
